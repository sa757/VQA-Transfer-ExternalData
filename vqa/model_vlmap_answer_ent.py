import cPickle
import h5py
import os
import numpy as np
import tensorflow as tf

from util import log
from vlmap import modules

W_DIM = 300  # Word dimension
L_DIM = 1024  # Language dimension
V_DIM = 1024

W_ENTROPY = 0.1

NUM_MARGINAL = 200


class Model(object):

    def __init__(self, batch, config, is_train=True):
        self.batch = batch
        self.config = config
        self.image_dir = config.image_dir
        self.is_train = is_train

        self.batch_size = config.batch_size

        self.word_weight_dir = getattr(config, 'vlmap_word_weight_dir', None)
        if self.word_weight_dir is None:
            log.warn('word_weight_dir is None')

        self.losses = {}
        self.report = {}
        self.mid_result = {}
        self.output = {}
        self.vis_image = {}

        self.vocab = cPickle.load(open(config.vocab_path, 'rb'))
        self.answer_dict = cPickle.load(open(
            os.path.join(config.tf_record_dir, 'answer_dict.pkl'), 'rb'))
        self.num_answer = len(self.answer_dict['vocab'])
        self.num_train_answer = self.answer_dict['num_train_answer']
        self.train_answer_mask = tf.expand_dims(tf.sequence_mask(
            self.num_train_answer, maxlen=self.num_answer, dtype=tf.float32),
            axis=0)
        self.test_answer_mask = 1.0 - self.train_answer_mask
        self.train_answer_mask_bool = tf.greater(
            tf.squeeze(self.train_answer_mask, axis=0), 0.5)
        self.obj_answer_mask = tf.expand_dims(
            tf.constant(self.answer_dict['is_object'], dtype=tf.float32),
            axis=0)
        self.attr_answer_mask = tf.expand_dims(
            tf.constant(self.answer_dict['is_attribute'], dtype=tf.float32),
            axis=0)

        self.glove_map = modules.LearnGloVe(self.vocab)
        self.answer_exist_mask = modules.AnswerExistMask(
            self.answer_dict, self.word_weight_dir)
        self.answer_exist_mask_bool = tf.greater(
            tf.squeeze(self.answer_exist_mask, axis=0), 0.5)

        self.train_exist_answer_mask_bool = tf.greater(
            tf.squeeze(self.answer_exist_mask * self.train_answer_mask, axis=0),
            0.5)

        log.infov('loading image features...')
        with h5py.File(config.vfeat_path, 'r') as f:
            self.features = np.array(f.get('image_features'))
            log.infov('feature done')
            self.spatials = np.array(f.get('spatial_features'))
            log.infov('spatials done')
            self.normal_boxes = np.array(f.get('normal_boxes'))
            log.infov('normal_boxes done')
            self.num_boxes = np.array(f.get('num_boxes'))
            log.infov('num_boxes done')
            self.max_box_num = int(f['data_info']['max_box_num'].value)
            self.vfeat_dim = int(f['data_info']['vfeat_dim'].value)
        log.infov('done')

        self.build()

    def filter_train_vars(self, trainable_vars):
        train_vars = []
        for var in trainable_vars:
            if var.name.split('/')[0] == 'q_linear_l': pass
            elif var.name.split('/')[0] == 'pooled_linear_l': pass
            elif var.name.split('/')[0] == 'joint_fc': pass
            elif var.name.split('/')[0] == 'WordWeightAnswer': pass
            else: train_vars.append(var)
        return train_vars

    def filter_transfer_vars(self, all_vars):
        transfer_vars = []
        for var in all_vars:
            if var.name.split('/')[0] == 'q_linear_l':
                transfer_vars.append(var)
            elif var.name.split('/')[0] == 'pooled_linear_l':
                transfer_vars.append(var)
            elif var.name.split('/')[0] == 'joint_fc':
                transfer_vars.append(var)
        return transfer_vars

    def build(self):
        """
        build network architecture and loss
        """

        """
        Visual features
        """
        with tf.device('/cpu:0'):
            def load_feature(image_idx):
                selected_features = np.take(self.features, image_idx, axis=0)
                return selected_features
            V_ft = tf.py_func(
                load_feature, inp=[self.batch['image_idx']], Tout=tf.float32,
                name='sample_features')
            V_ft.set_shape([None, self.max_box_num, self.vfeat_dim])
            num_V_ft = tf.gather(self.num_boxes, self.batch['image_idx'],
                                 name='gather_num_V_ft', axis=0)
            self.mid_result['num_V_ft'] = num_V_ft
            normal_boxes = tf.gather(self.normal_boxes, self.batch['image_idx'],
                                     name='gather_normal_boxes', axis=0)
            self.mid_result['normal_boxes'] = normal_boxes

        log.warning('v_linear_v')
        v_linear_v = modules.fc_layer(
            V_ft, V_DIM, use_bias=True, use_bn=False, use_ln=True,
            activation_fn=tf.nn.relu, is_training=self.is_train,
            scope='v_linear_v')

        """
        Encode question
        """
        q_embed = tf.nn.embedding_lookup(self.glove_map, self.batch['q_intseq'])
        # [bs, L_DIM]
        q_L_ft = modules.encode_L(q_embed, self.batch['q_intseq_len'], L_DIM,
                                  cell_type='GRU')

        # [bs, V_DIM}
        log.warning('q_linear_v')
        q_linear_v = modules.fc_layer(
            q_L_ft, V_DIM, use_bias=True, use_bn=False, use_ln=True,
            activation_fn=tf.nn.relu, is_training=self.is_train,
            scope='q_linear_v')
        self.mid_result['q_linear_v'] = q_linear_v

        """
        Perform attention
        """
        att_score = modules.hadamard_attention(
            v_linear_v, num_V_ft, q_linear_v,
            use_ln=False, is_train=self.is_train)
        self.output['att_score'] = att_score
        self.mid_result['att_score'] = att_score
        pooled_V_ft = modules.attention_pooling(V_ft, att_score)
        self.mid_result['pooled_V_ft'] = pooled_V_ft

        """
        Answer classification
        """
        log.warning('pooled_linear_l')
        pooled_linear_l = modules.fc_layer(
            pooled_V_ft, L_DIM, use_bias=True, use_bn=False, use_ln=True,
            activation_fn=tf.nn.relu, is_training=self.is_train,
            scope='pooled_linear_l')
        self.mid_result['pooled_linear_l'] = pooled_linear_l

        log.warning('q_linear_l')
        l_linear_l = modules.fc_layer(
            q_L_ft, L_DIM, use_bias=True, use_bn=False, use_ln=True,
            activation_fn=tf.nn.relu, is_training=self.is_train,
            scope='q_linear_l')
        self.mid_result['l_linear_l'] = l_linear_l

        joint = modules.fc_layer(
            pooled_linear_l * l_linear_l, L_DIM * 2,
            use_bias=True, use_bn=False, use_ln=True,
            activation_fn=tf.nn.relu, is_training=self.is_train, scope='joint_fc')
        joint = tf.nn.dropout(joint, 0.5)
        self.mid_result['joint'] = joint

        logit = modules.WordWeightAnswer(
            joint, self.answer_dict, self.word_weight_dir,
            use_bias=True, is_training=self.is_train, scope='WordWeightAnswer')
        self.output['logit'] = logit
        self.mid_result['logit'] = logit

        """
        Maximum entropy regularization
        """
        # pooled_linear_l [bs, L_DIM]
        # l_linear_l [bs, L_DIM]
        tile_pooled_linear_l = tf.reshape(
            tf.tile(tf.stop_gradient(pooled_linear_l), [NUM_MARGINAL, 1]),
            [-1, NUM_MARGINAL, L_DIM])
        tile_l_linear_l = tf.expand_dims(l_linear_l, axis=1)  # use broadcasting

        tile_joint = modules.fc_layer(  # [bs, NUM_MARGINAL, L_DIM]
            tile_pooled_linear_l * tile_l_linear_l, L_DIM * 2,
            use_bias=True, use_bn=False, use_ln=True,
            activation_fn=tf.nn.relu, is_training=self.is_train, scope='joint_fc')
        tile_joint = tf.nn.dropout(tile_joint, 0.5)

        tile_logit = modules.WordWeightAnswer(  # [bs, NUM_MARGINAL, # answer]
            tile_joint, self.answer_dict, self.word_weight_dir,
            use_bias=True, is_training=self.is_train, scope='WordWeightAnswer')
        masked_tile_logit = tf.boolean_mask(  # [bs, NUM_MARGINAL, # train answer]
            tile_logit, self.train_exist_answer_mask_bool, axis=2)
        tile_prob = tf.nn.softmax(masked_tile_logit, axis=-1)  # [bs, NUM_MARGINAL, # train answer]

        marginal_prob = tf.reduce_mean(tile_prob, axis=1)  # [bs, # train answer]

        """
        Compute loss and accuracy
        """
        with tf.name_scope('loss'):
            answer_target = self.batch['answer_target']
            loss = tf.nn.sigmoid_cross_entropy_with_logits(
                labels=answer_target, logits=logit)

            train_loss = tf.reduce_mean(tf.reduce_sum(
                loss * self.train_answer_mask, axis=-1))
            report_loss = tf.reduce_mean(tf.reduce_sum(loss, axis=-1))
            pred = tf.cast(tf.argmax(logit, axis=-1), dtype=tf.int32)
            one_hot_pred = tf.one_hot(pred, depth=self.num_answer,
                                      dtype=tf.float32)
            acc = tf.reduce_mean(
                tf.reduce_sum(one_hot_pred * answer_target, axis=-1))
            exist_acc = tf.reduce_mean(
                tf.reduce_sum(one_hot_pred * answer_target * self.answer_exist_mask,
                              axis=-1))
            test_acc = tf.reduce_mean(
                tf.reduce_sum(one_hot_pred * answer_target * self.test_answer_mask,
                              axis=-1))
            test_obj_acc = tf.reduce_mean(
                tf.reduce_sum(one_hot_pred * answer_target * self.test_answer_mask *
                              self.obj_answer_mask, axis=-1))
            test_attr_acc = tf.reduce_mean(
                tf.reduce_sum(one_hot_pred * answer_target * self.test_answer_mask *
                              self.attr_answer_mask, axis=-1))
            train_exist_acc = tf.reduce_mean(
                tf.reduce_sum(one_hot_pred * answer_target * self.answer_exist_mask *
                              self.train_answer_mask,
                              axis=-1))
            max_exist_answer_acc = tf.reduce_mean(
                tf.reduce_max(answer_target * self.answer_exist_mask, axis=-1))
            max_train_exist_acc = tf.reduce_mean(
                tf.reduce_max(answer_target * self.answer_exist_mask *
                              self.train_answer_mask, axis=-1))
            test_obj_max_acc = tf.reduce_mean(
                tf.reduce_max(answer_target * self.test_answer_mask *
                              self.obj_answer_mask, axis=-1))
            test_attr_max_acc = tf.reduce_mean(
                tf.reduce_max(answer_target * self.test_answer_mask *
                              self.attr_answer_mask, axis=-1))
            test_max_answer_acc = tf.reduce_mean(
                tf.reduce_max(answer_target * self.test_answer_mask, axis=-1))
            test_max_exist_answer_acc = tf.reduce_mean(
                tf.reduce_max(answer_target * self.answer_exist_mask *
                              self.test_answer_mask, axis=-1))
            normal_test_obj_acc = tf.where(
                tf.equal(test_obj_max_acc, 0),
                test_obj_max_acc,
                test_obj_acc / test_obj_max_acc)
            normal_test_attr_acc = tf.where(
                tf.equal(test_attr_max_acc, 0),
                test_attr_max_acc,
                test_attr_acc / test_attr_max_acc)
            normal_train_exist_acc = tf.where(
                tf.equal(max_train_exist_acc, 0),
                max_train_exist_acc,
                train_exist_acc / max_train_exist_acc)
            normal_exist_acc = tf.where(
                tf.equal(max_exist_answer_acc, 0),
                max_exist_answer_acc,
                exist_acc / max_exist_answer_acc)
            normal_test_acc = tf.where(
                tf.equal(test_max_answer_acc, 0),
                test_max_answer_acc,
                test_acc / test_max_answer_acc)

            marginal_logprob = tf.log(marginal_prob + 1e-8)
            negative_entropy = tf.reduce_mean(
                tf.reduce_sum(marginal_prob * marginal_logprob, axis=-1))
            w_negative_entropy = W_ENTROPY * negative_entropy

            self.mid_result['pred'] = pred

            self.losses['answer'] = train_loss
            self.losses['entropy'] = w_negative_entropy
            self.report['entropy'] = negative_entropy
            self.report['weighted_entropy'] = w_negative_entropy
            self.report['answer_train_loss'] = train_loss
            self.report['answer_report_loss'] = report_loss
            self.report['answer_acc'] = acc
            self.report['exist_acc'] = exist_acc
            self.report['test_acc'] = test_acc
            self.report['normal_test_acc'] = normal_test_acc
            self.report['normal_test_object_acc'] = normal_test_obj_acc
            self.report['normal_test_attribute_acc'] = normal_test_attr_acc
            self.report['normal_exist_acc'] = normal_exist_acc
            self.report['normal_train_exist_acc'] = normal_train_exist_acc
            self.report['max_exist_acc'] = max_exist_answer_acc
            self.report['test_max_acc'] = test_max_answer_acc
            self.report['test_max_exist_acc'] = test_max_exist_answer_acc

        """
        Prepare image summary
        """
        """
        with tf.name_scope('prepare_summary'):
            self.vis_image['image_attention_qa'] = self.visualize_vqa_result(
                self.batch['image_id'],
                self.mid_result['normal_boxes'], self.mid_result['num_V_ft'],
                self.mid_result['att_score'],
                self.batch['q_intseq'], self.batch['q_intseq_len'],
                self.batch['answer_target'], self.mid_result['pred'],
                max_batch_num=20, line_width=2)
        """

        self.loss = 0
        for key, loss in self.losses.items():
            self.loss = self.loss + loss

        # scalar summary
        for key, val in self.report.items():
            tf.summary.scalar('train/{}'.format(key), val,
                              collections=['heavy_train', 'train'])
            tf.summary.scalar('val/{}'.format(key), val,
                              collections=['heavy_val', 'val'])
            tf.summary.scalar('testval/{}'.format(key), val,
                              collections=['heavy_testval', 'testval'])

        # image summary
        for key, val in self.vis_image.items():
            tf.summary.image('train-{}'.format(key), val, max_outputs=10,
                             collections=['heavy_train'])
            tf.summary.image('val-{}'.format(key), val, max_outputs=10,
                             collections=['heavy_val'])
            tf.summary.image('testval-{}'.format(key), val, max_outputs=10,
                             collections=['heavy_testval'])

        return self.loss
