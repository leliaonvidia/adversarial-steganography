import functools
import os
import time

import numpy as np
import tensorflow as tf
from tensorflow.contrib.layers import convolution2d as conv2d
from tensorflow.contrib.layers import fully_connected as linear

from advstego.nn import BaseModel
from advstego.nn import get_image
from advstego.utils import logger, log


def lazy_property(function):
    attribute = '_' + function.__name__

    @property
    @functools.wraps(function)
    def wrapper(self):
        if not hasattr(self, attribute):
            setattr(self, attribute, function(self))
        return getattr(self, attribute)

    return wrapper


class Steganalyzer(BaseModel):
    def __init__(self, config, sess, stego_algorithm, image_shape=(64, 64, 3), stego_name=None):
        super().__init__(sess, config)
        self.stego_algorithm = stego_algorithm
        self.image_shape = image_shape
        self.stego_name = stego_name

        self.images = tf.placeholder(tf.float32, [self.conf.batch_size] + list(self.image_shape))
        self.target = tf.placeholder(tf.float32, [self.conf.batch_size, 2])

        if stego_name:
            self.data = self.get_images_names('%s_train/*.%s' % (stego_name, self.conf.img_format))
            self.test_dir = '%s_test' % stego_name
        else:
            self.data = self.get_images_names('train/*.%s' % self.conf.img_format)
            self.test_dir = 'test'

        # init
        self.loss
        self.optimize
        self.network

    def image_processing_layer(self, X):
        K = 1 / 12. * tf.constant([
            [-1, 2, -2, 2, -1],
            [2, -6, 8, -6, 2],
            [-2, 8, -12, 8, -2],
            [2, -6, 8, -6, 2],
            [-1, 2, -2, 2, -1]
        ], dtype=tf.float32)

        kernel = tf.pack([K, K, K])
        kernel = tf.pack([kernel, kernel, kernel])

        return tf.nn.conv2d(X, tf.transpose(kernel, [2, 3, 0, 1]), [1, 1, 1, 1], padding='SAME')

    def get_targets(self, batch_files):
        get_tar = lambda x: int('stego_' in os.path.split(x)[-1])
        targets = np.array([get_tar(f) for f in batch_files], dtype=np.int32)
        out = np.zeros((self.conf.batch_size, 2), dtype=np.float32)
        out[range(targets.shape[0]), targets] = 1.
        # print(targets)
        return out

    @log('Training')
    def train(self, counter=1, gen_dirs=()):
        if self.conf.need_to_load:
            self.load(self.conf.checkpoint_dir, step=counter)

        data = self.data
        logger.info('Total amount of images: %s' % len(data))
        # np.random.shuffle(data)

        tf.initialize_all_variables().run()

        # counter = 1
        start_time = time.time()
        batch_idxs = min(len(data), self.conf.train_size) / self.conf.batch_size

        stego_accuracy = 0

        accuracies = []
        accuracies_steps = []

        logger.debug('Starting updating')
        for epoch in range(self.conf.epoch):
            losses = []

            np.random.shuffle(data)

            logger.info('Starting epoch %s' % epoch)

            for idx in range(0, int(batch_idxs)):
                batch_files = data[idx * self.conf.batch_size:(idx + 1) * self.conf.batch_size]
                batch = [get_image(batch_file, self.conf.image_size)
                         for batch_file in batch_files]
                batch_images = np.array(batch).astype(np.float32)

                batch_targets = self.get_targets(batch_files)

                self.sess.run(self.optimize, feed_dict={self.images: batch_images, self.target: batch_targets})
                loss = self.loss.eval({self.images: batch_images, self.target: batch_targets})

                losses.append(loss)

                # logger.debug("[ITERATION] Epoch [%2d], iteration [%4d/%4d] time: %4.4f, Loss: %8f, accuracy: %8f" %
                #              (epoch, idx, batch_idxs, time.time() - start_time, loss, stego_accuracy))

                counter += 1

                if counter % 300 == 0:
                    logger.info('------')

                    stego_accuracy = self.accuracy(n_files=-1, test_dir=self.test_dir)
                    logger.info('[TEST] Epoch {:2d} accuracy: {:3.1f}%'.format(epoch + 1, 100 * stego_accuracy))

                    for gen_dir in gen_dirs:
                        gen_accuracy = self.accuracy(n_files=-1, test_dir=gen_dir)
                        logger.info('[GEN_TEST] Folder {}, accuracy: {:3.1f}%'.format(gen_dir, 100 * gen_accuracy))



                        # SAVE after each epoch
                        # self.save(self.conf.checkpoint_dir, counter)

                        # stego_accuracy = self.accuracy(n_files=-1, test_dir=self.test_dir)
                        # gen_accuracy = self.accuracy(n_files=-1, test_dir=gen_dir)
                        #
                        # accuracies.append(stego_accuracy)
                        # accuracies_steps.append(counter)
                        #
                        # np.savetxt('accuracies.csv', accuracies)
                        # np.savetxt('accuracies_steps.csv', accuracies_steps)
                        #
                        # max_acc_idx = np.argmax(accuracies)
                        # logger.info('[TEST] Epoch {:2d} error: {:3.1f}%'.format(epoch + 1, 100 * stego_accuracy))
                        # logger.info('[GEN_TEST] Epoch {:2d} error: {:3.1f}%'.format(epoch + 1, 100 * gen_accuracy))

                        # logger.info('[TEST] Max accuracy: %s, step: %s, epoch: %s' % (accuracies[max_acc_idx],
                        #                                                               accuracies_steps[max_acc_idx], epoch))

    def get_accuracy(self, X_test, y_test):
        stego_answs = self.sess.run(self.network, feed_dict={self.images: X_test})
        stego_mistakes = tf.equal(tf.argmax(y_test, 1), tf.argmax(stego_answs, 1))

        return tf.reduce_mean(tf.cast(stego_mistakes, tf.float32)).eval()

    def accuracy(self, test_dir='test', abs=False, n_files=2 ** 12):
        logger.info('[TEST], test data folder: %s, n_files: %s' % (test_dir, 2 * n_files))
        X_test = self.get_images_names('%s/*.%s' % (test_dir, self.conf.img_format), abs=abs)[:n_files]

        accuracies = []

        batch_idxs = min(len(X_test), self.conf.train_size) / self.conf.batch_size

        # logger.debug('Starting iteration')
        for idx in range(0, int(batch_idxs)):
            batch_files_stego = X_test[idx * self.conf.batch_size:(idx + 1) * self.conf.batch_size]
            batch = [get_image(batch_file, self.conf.image_size) for batch_file in batch_files_stego]
            batch_images = np.array(batch).astype(np.float32)

            batch_targets = self.get_targets(batch_files_stego)

            accuracies.append(self.get_accuracy(batch_images, batch_targets))

        return np.mean(accuracies)

    @lazy_property
    def saver(self):
        return tf.train.Saver()

    @lazy_property
    def loss(self):
        errs = tf.nn.softmax_cross_entropy_with_logits(self.network, self.target)
        return tf.reduce_mean(errs)

    @lazy_property
    def optimize(self):
        return tf.train.AdamOptimizer(self.conf.learning_rate, beta1=self.conf.beta1).minimize(self.loss)

    @lazy_property
    def network(self):
        net = self.images

        net = self.image_processing_layer(net)

        def get_init():
            return tf.truncated_normal_initializer(stddev=0.02)

        net = conv2d(net, 10, [7, 7], activation_fn=tf.nn.relu, name='conv1', weights_initializer=get_init())
        net = conv2d(net, 20, [5, 5], activation_fn=tf.nn.relu, name='conv2', weights_initializer=get_init())
        net = tf.nn.max_pool(net, [1, 4, 4, 1], [1, 1, 1, 1], padding='SAME')

        net = conv2d(net, 30, [3, 3], activation_fn=tf.nn.relu, name='conv3', weights_initializer=get_init())
        net = conv2d(net, 40, [3, 3], activation_fn=tf.nn.relu, name='conv4', weights_initializer=get_init())

        net = tf.nn.max_pool(net, [1, 2, 2, 1], [1, 1, 1, 1], padding='SAME')

        net = tf.reshape(net, [self.conf.batch_size, -1])

        net = linear(net, 100, activation_fn=tf.nn.tanh, name='FC1')
        out = linear(net, 2, activation_fn=tf.nn.softmax, name='out')
        return out
