from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import numpy as np
import tensorflow as tf
from openkim_fit.dataset import DataSet
from openkim_fit.descriptor import Descriptor
from tensorflow.contrib.layers import (
    fully_connected,
    xavier_initializer,
    variance_scaling_initializer,
)
import openkim_fit.ann as ann

# Check that the forces and energys are the same with the KIM model is used.
# 0. To test dropout, modify `dropout_` function in `network.cpp` to set `debug` to true,
#    where the dropout is hard-coded to be the same as the one here.
#    To not test dropout, do not need to change it.
# 1. run this script
# 2. copy `ann_kim.params' generated by this script to the KIM Carbon model, and make.
#    Note, if `struct == bilayer`, copy to `Artificial_Neural_Network_C__MO_000000111111_000`
#    elif `struct == bulk`, copy to `Artificial_Neural_Network_Intralayer_C__MO_000000111111_000`.
# 3. run LAMMPS with input `lammps_graphene_bilayer.in` or `lammps_graphene_monolayer.in' to generate
#    the dump file. (located at bop  /usr/scratch/wenxx151/Documents/lmp_playground/debug_ann)
# 4. compare the stdout of this script and the LAMMPS dump file.
# 5. Agreement between models you want to check when this test does not pass.
# 1) Both use the same STRUCT.
# 2) This script will write the not normalized descriptor values to file `debug_descriptor.txt`,
#    and the normalized descriptor values to `gc_normalized.txt`.
#    It can be compared with the one generated by the model driver. TO enable it, search `TODO`
#    in ANNImplementation.hpp.
#    Note, look carefully for the order of the descriptor values for each atom. They may be different
#    from this script and the KIM model. Here, in function `dropout` we are modifying those of the first
#    3 atoms. If the order of the descriptor value of the first threee atoms are differert, different
#    results will be obtained.


# set a global random seed
tf.set_random_seed(1)


##############################
# settings
# DO_NORMALIZE = False
DO_NORMALIZE = True
# DO_DROPOUT = False
DO_DROPOUT = True
STRUCT = 'bulk'
# STRUCT = 'bilayer'
##############################

if DO_DROPOUT == True:
    keep_prob = [0.9, 1, 0.7]
else:
    keep_prob = [1.0, 1.0, 1.0]


def dropout(x, keep_prob):
    if keep_prob < 1 - 1e-10:
        binary = np.ones(x.shape)
        binary[0][0] = 0.0
        binary[0][5] = 0.0
        binary[1][2] = 0.0
        binary[1][7] = 0.0
        binary[2][2] = 0.0
        binary[2][5] = 0.0
        binary = tf.constant(binary)
        return (x / keep_prob) * binary
    else:
        return x


# create Descriptor
cutfunc = 'cos'
cutvalue = {'C-C': 5.0}
# cutvalue = {'Mo-Mo':5., 'Mo-S':5., 'S-S':5.}
desc_params = {
    'g1': None,
    'g2': [{'eta': 0.1, 'Rs': 0.2}, {'eta': 0.3, 'Rs': 0.4}],
    'g3': [{'kappa': 0.1}, {'kappa': 0.2}, {'kappa': 0.3}],
    'g4': [
        {'zeta': 0.1, 'lambda': 0.2, 'eta': 0.01},
        {'zeta': 0.3, 'lambda': 0.4, 'eta': 0.02},
    ],
    'g5': [
        {'zeta': 0.11, 'lambda': 0.22, 'eta': 0.011},
        {'zeta': 0.33, 'lambda': 0.44, 'eta': 0.022},
    ],
}

desc = Descriptor(desc_params, cutfunc, cutvalue, cutvalue_samelayer=cutvalue, debug=True)
num_desc = desc.get_num_descriptors()


# read config and reference data
tset = DataSet()
if STRUCT == 'bilayer':
    tset.read('./training_set/graphene_bilayer_1x1.xyz')
else:
    tset.read('./training_set/graphene_monolayer_2x2.xyz')
configs = tset.get_configs()


# preprocess data to generate tfrecords
DTYPE = tf.float64
train_name, _ = ann.convert_to_tfrecord(
    configs,
    desc,
    size_validation=0,
    directory='/tmp',
    do_generate=True,
    do_normalize=DO_NORMALIZE,
    do_shuffle=False,
    use_welford=False,
    fit_forces=True,
    structure=STRUCT,
    dtype=DTYPE,
)

# read data from tfrecords into tensors
dataset = ann.read_tfrecord(train_name, fit_forces=True, dtype=DTYPE)

# number of epoches
iterator = dataset.make_one_shot_iterator()


# create NN
initializer = ann.weight_decorator(xavier_initializer(dtype=DTYPE))
size = 20
subloss = []

name, num_atoms_by_species, weight, gen_coords, energy_label, atomic_coords, dgen_datomic_coords, forces_label = (
    iterator.get_next()
)

in_layer = ann.input_layer_given_data(
    atomic_coords, gen_coords, dgen_datomic_coords, num_descriptor=num_desc
)
in_layer.set_shape((configs[0].get_num_atoms(), num_desc))
in_layer_drop = dropout(in_layer, keep_prob[0])


hidden1 = fully_connected(
    in_layer_drop,
    size,
    activation_fn=tf.nn.tanh,
    weights_initializer=initializer,
    biases_initializer=tf.truncated_normal_initializer(dtype=DTYPE),
    scope='hidden1',
)
hidden1_drop = dropout(hidden1, keep_prob[1])

hidden2 = fully_connected(
    hidden1_drop,
    size,
    activation_fn=tf.nn.tanh,
    weights_initializer=initializer,
    biases_initializer=tf.truncated_normal_initializer(dtype=DTYPE),
    scope='hidden2',
)
hidden2_drop = dropout(hidden2, keep_prob[2])

output = fully_connected(
    hidden2_drop,
    1,
    activation_fn=None,
    weights_initializer=initializer,
    biases_initializer=tf.truncated_normal_initializer(dtype=DTYPE),
    scope='output',
)


# energy and forces
energy = tf.reduce_sum(output)
# tf.gradients return a LIST of tensors
forces = -tf.gradients(output, atomic_coords)[0]

weights, biases = ann.get_weights_and_biases(['hidden1', 'hidden2', 'output'])


with tf.Session() as sess:

    # init global vars
    init_op = tf.global_variables_initializer()
    sess.run(init_op)

    en, fo = sess.run([energy, forces])
    print('energy:', en)
    print('forces:')
    for i, f in enumerate(fo):
        print('{:13.5e}'.format(f), end='')
        if i % 3 == 2:
            print()

    # output results to a KIM model
    w, b = sess.run([weights, biases])
    ann.write_kim_ann(desc, w, b, tf.nn.tanh, keep_prob=keep_prob, dtype=tf.float64)


# write normalized gcc
tfrecord_name = '/tmp/train.tfrecord'
text_name = 'gc_normalized.txt'
ann.tfrecord_to_text(tfrecord_name, text_name, fit_forces=True, dtype=tf.float64)
