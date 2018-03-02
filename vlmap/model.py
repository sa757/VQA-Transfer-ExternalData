import h5py
import json
import numpy as np
import tensorflow as tf
import tensorflow.contrib.slim as slim
import tensorflow.contrib.slim.nets as nets
import tensorflow.contrib.seq2seq as seq2seq

from util import log
from vlmap import modules

TOP_K = 5
W_DIM = 300  # Word dimension
L_DIM = 384  # Language dimension
MAP_DIM = 384
V_DIM = 384
ENC_I_PARAM_PATH = 'data/nets/resnet_v1_50.ckpt'


class Model(object):

    def __init__(self, batches, config, is_train=True):
        self.batches = batches
        self.config = config

        self.report = {}
        self.output = {}

        self.no_object = config.no_object
        self.no_region = config.no_region

        self.object_batch_size = config.object_batch_size
        self.region_batch_size = config.region_batch_size

        self.object_num_k = config.object_num_k
        self.object_max_name_len = config.object_max_name_len

        self.region_max_len = config.region_max_len

        self.wordset = modules.used_wordset(config.used_wordset_path)

        self.glove_all = modules.glove_embedding_map()
        self.glove_wordset = tf.nn.embedding_lookup(self.glove_all,
                                                    self.wordset)
        self.word_predictor = modules.WordPredictor(self.glove_wordset,
                                                    trainable=is_train,
                                                    name='WordPredictor')

        self.vocab = json.load(open(config.vocab_path, 'r'))
        self.wordset_vocab = {}
        with h5py.File(config.used_wordset_path, 'r') as f:
            wordset = list(f['used_wordset'].value)
            self.wordset_vocab['vocab'] = [self.vocab['vocab'][w]
                                           for w in wordset]
            self.wordset_vocab['dict'] = {w: i for i, w in
                                          enumerate(self.wordset_vocab['vocab'])}

        # model parameters
        self.finetune_enc_I = config.finetune_enc_I
        self.no_V_grad_enc_L = config.no_V_grad_enc_L
        self.no_V_grad_dec_L = config.no_V_grad_dec_L

        self.build(is_train=is_train)

    def filter_vars(self, all_vars):
        enc_I_vars = []
        learn_v_vars = []  # variables learning from vision loss
        learn_l_vars = []  # variables learning from language loss
        for var in all_vars:
            if var.name.split('/')[0] == 'resnet_v1_50':
                enc_I_vars.append(var)
                if self.finetune_enc_I:
                    learn_v_vars.append(var)
            elif var.name.split('/')[0] == 'language_encoder':
                learn_l_vars.append(var)
                if not self.no_V_grad_enc_L:
                    learn_v_vars.append(var)
            elif var.name.split('/')[0] == 'language_decoder':
                learn_l_vars.append(var)
                if not self.no_V_grad_dec_L:
                    learn_v_vars.append(var)
            else:
                learn_v_vars.append(var)
        return enc_I_vars, learn_v_vars, learn_l_vars

    def get_enc_I_param_path(self):
        return ENC_I_PARAM_PATH

    def visualize_word_prediction(self, logit, label):
        prob = tf.nn.softmax(logit, dim=-1)
        prob_image = tf.expand_dims(prob, axis=-1)
        label_image = tf.expand_dims(label, axis=-1)
        dummy = tf.zeros_like(label_image)
        pred_image = tf.clip_by_value(
            tf.concat([prob_image, label_image, dummy], axis=-1),
            0, 1)
        pred_image = tf.reshape(
            tf.tile(pred_image, [1, 10, 1]), [-1, self.object_num_k, 3])
        return tf.expand_dims(pred_image, axis=0)

    def visualize_word_prediction_text(self, logits, labels, names):
        label_token = tf.cast(tf.argmax(labels, axis=-1), tf.int32)
        _, top_k_pred = tf.nn.top_k(logits, k=TOP_K)
        batch_range = tf.expand_dims(
            tf.range(0, tf.shape(label_token)[0], delta=1), axis=1)
        range_label_token = tf.concat(
            [batch_range, tf.expand_dims(label_token, axis=1)], axis=1)
        label_name = tf.gather_nd(names, range_label_token)
        top_k_preds = tf.split(axis=-1, num_or_size_splits=TOP_K,
                               value=top_k_pred)
        pred_names = []
        for i in range(TOP_K):
            range_top_k_pred = tf.concat(
                [batch_range, top_k_preds[i]], axis=1)
            pred_names.append(tf.gather_nd(names, range_top_k_pred))
        string_list = ['gt: ', label_name]
        for i in range(TOP_K):
            string_list.extend([', pred({}): '.format(i), pred_names[i]])
        pred_string = tf.string_join(string_list)
        return pred_string

    def word_prediction_loss(self, logits, labels):
        cross_entropy = tf.nn.softmax_cross_entropy_with_logits_v2(
            labels=tf.stop_gradient(labels), logits=logits)
        loss = tf.reduce_mean(cross_entropy)
        # Accuracy
        label_token = tf.cast(tf.argmax(labels, axis=-1), tf.int32)
        logit_token = tf.cast(tf.argmax(logits, axis=-1), tf.int32)
        acc = tf.reduce_mean(tf.to_float(
            tf.equal(label_token, logit_token)))
        _, top_k_pred = tf.nn.top_k(logits, k=TOP_K)
        k_label_token = tf.tile(
            tf.expand_dims(label_token, axis=1), [1, TOP_K])
        top_k_acc = tf.reduce_mean(tf.to_float(tf.reduce_any(
            tf.equal(k_label_token, top_k_pred), axis=1)))
        return loss, acc, top_k_acc

    def sequence_accuracy(self, pred, pred_len, seq, seq_len):
        max_len = tf.reduce_max(tf.concat([pred_len, seq_len], axis=0))
        # Dynamic padding
        p_sz = tf.shape(pred)
        p_pad = tf.zeros([p_sz[0], max_len - p_sz[1]], dtype=pred.dtype)
        s_sz = tf.shape(seq)
        s_pad = tf.zeros([s_sz[0], max_len - s_sz[1]], dtype=seq.dtype)
        pred = tf.concat([pred, p_pad], axis=1)
        seq = tf.concat([seq, s_pad], axis=1)
        # Mask construction
        mask = tf.sequence_mask(seq_len, maxlen=max_len,
                                dtype=tf.float32, name='mask')
        min_mask = tf.sequence_mask(tf.minimum(pred_len, seq_len),
                                    maxlen=max_len,
                                    dtype=tf.float32, name='min_mask')
        max_mask = tf.sequence_mask(tf.maximum(pred_len, seq_len),
                                    maxlen=max_len,
                                    dtype=tf.float32, name='max_mask')
        # Accuracy
        token_acc = tf.reduce_sum(tf.to_float(tf.equal(
            pred, seq)) * min_mask) / tf.reduce_sum(max_mask, axis=[0, 1])
        seq_acc = tf.reduce_mean(tf.to_float(tf.logical_and(
            tf.reduce_all(tf.equal(pred * tf.cast(mask, tf.int32),
                                   seq * tf.cast(mask, tf.int32)), axis=-1),
            tf.equal(seq_len, pred_len))))
        return token_acc, seq_acc

    def seq2string(self, seq, seq_len):
        def seq2string_fn(in_seq, in_seq_len):
            seq_strings = []
            for s, s_len in zip(in_seq, in_seq_len):
                seq_strings.append(
                    ' '.join([self.wordset_vocab['vocab'][i]
                              for i in s[:s_len]]))
            return np.array(seq_strings)
        seq_string = tf.py_func(seq2string_fn, [seq, seq_len], tf.string)
        return seq_string

    def add_classification_summary(self, loss, acc, top_k_acc, image,
                                   pred_image, pred_string, name='default'):
        tf.summary.scalar('train-{}/loss'.format(name),
                          loss, collections=['train'])
        tf.summary.scalar('val-{}/loss'.format(name), loss, collections=['val'])

        tf.summary.scalar('train-{}/accuracy'.format(name),
                          acc, collections=['train'])
        tf.summary.scalar('val-{}/accuracy'.format(name),
                          acc, collections=['val'])

        tf.summary.scalar('train-{}/top_{}_acc'.format(name, TOP_K),
                          top_k_acc, collections=['train'])
        tf.summary.scalar('val-{}/top_{}_acc'.format(name, TOP_K),
                          top_k_acc, collections=['val'])
        tf.summary.image('train-{}_image'.format(name),
                         image, collections=['train'])
        tf.summary.image('train-{}_prediction_image'.format(name), pred_image,
                         collections=['train'])
        tf.summary.image('val-{}_image'.format(name),
                         image, collections=['val'])
        tf.summary.image('val-{}_prediction_image'.format(name), pred_image,
                         collections=['val'])
        tf.summary.text('train-{}_pred_string'.format(name),
                        pred_string, collections=['train'])
        tf.summary.text('val-{}_pred_string'.format(name),
                        pred_string, collections=['val'])

    def build_object(self, is_train=True):
        # feat_V
        enc_I = modules.encode_I(self.batches['object']['image'],
                                 is_train=self.finetune_enc_I,
                                 reuse=tf.AUTO_REUSE)
        if not self.finetune_enc_I: enc_I = tf.stop_gradient(enc_I)
        feat_V = modules.I2V(enc_I, MAP_DIM, V_DIM, scope='I2V',
                             is_train=is_train, reuse=tf.AUTO_REUSE)
        # enc_L: encode object classes
        embed_seq = tf.nn.embedding_lookup(self.glove_all,
                                           self.batches['object']['objects'])
        enc_L_flat = modules.language_encoder(
            tf.reshape(embed_seq, [-1, self.object_max_name_len, W_DIM]),
            tf.reshape(self.batches['object']['objects_len'], [-1]),
            L_DIM, scope='language_encoder', reuse=tf.AUTO_REUSE)
        enc_L = tf.reshape(enc_L_flat, [-1, self.object_num_k, L_DIM])
        if self.no_V_grad_enc_L: enc_L = tf.stop_gradient(enc_L)
        # L2V
        map_V = modules.L2V(enc_L, MAP_DIM, V_DIM, is_train=is_train,
                            scope='L2V', reuse=tf.AUTO_REUSE)
        # Classifier
        logits = modules.batch_word_classifier(
            feat_V, map_V, scope='object_classifier', reuse=tf.AUTO_REUSE)

        with tf.name_scope('ObjectClassLoss'):
            labels = self.batches['object']['ground_truth']
            loss, acc, top_k_acc = self.word_prediction_loss(
                logits, labels)
            self.report.update({'object-loss': loss, 'object-acc': acc,
                                'object-top_{}_acc'.format(TOP_K): top_k_acc})

        with tf.name_scope('Summary'):
            image = self.batches['object']['image'] / 255.0
            pred_image = self.visualize_word_prediction(logits, labels)
            pred_string = self.visualize_word_prediction_text(
                logits, labels, self.batches['object']['objects_name'])
            self.output.update({'object-image': image,
                                'object-prediction_string': pred_string})

        self.add_classification_summary(loss, acc, top_k_acc, image,
                                        pred_image, pred_string,
                                        name='object')
        return loss

    def build_region(self, is_train=True):
        # feat_V
        enc_I = modules.encode_I(self.batches['region']['image'],
                                 is_train=self.finetune_enc_I,
                                 reuse=tf.AUTO_REUSE)
        if not self.finetune_enc_I: enc_I = tf.stop_gradient(enc_I)
        feat_V = modules.I2V(enc_I, MAP_DIM, V_DIM, scope='I2V',
                             is_train=is_train, reuse=tf.AUTO_REUSE)
        # V2L [bs, L_DIM]
        map_L = modules.V2L(feat_V, MAP_DIM, L_DIM, scope='V2L',
                            is_train=is_train, reuse=tf.AUTO_REUSE)
        # Language inputs
        seq = self.batches['region']['region_description']
        seq_len = self.batches['region']['region_description_len']
        wordset_seq = self.batches['region']['wordset_region_description']
        # enc_L: "enc" in language enc-dec [bs, L_DIM]
        embed_seq = tf.nn.embedding_lookup(self.glove_all, seq)
        enc_L = modules.language_encoder(
            embed_seq, seq_len, L_DIM,
            scope='language_encoder', reuse=tf.AUTO_REUSE)
        # dec_L: "dec" in language enc-dec + description [2*bs, L_DIM]
        dbl_seq = tf.concat([seq, seq], axis=0)
        dbl_seq_len = tf.concat([seq_len, seq_len], axis=0)
        dbl_start_tokens = tf.zeros([tf.shape(dbl_seq)[0]], dtype=tf.int32) + \
            self.vocab['dict']['<s>']
        dbl_seq_with_start =  tf.concat([tf.expand_dims(dbl_start_tokens, axis=1),
                                         dbl_seq[:, :-1]], axis=1)
        dbl_embed_seq_with_start = tf.nn.embedding_lookup(self.glove_all,
                                                          dbl_seq_with_start)
        in_L = tf.concat([map_L, enc_L], axis=0)
        logits, pred, pred_len = modules.language_decoder(
            in_L, dbl_embed_seq_with_start,
            dbl_seq_len + 1,  # seq_len + 1 is for <s>
            lambda e: tf.nn.embedding_lookup(self.glove_wordset, e),
            W_DIM, dbl_start_tokens, self.vocab['dict']['<e>'],
            self.region_max_len + 1,  # + 1 for <e>
            unroll_type='teacher_forcing', output_layer=self.word_predictor,
            is_train=is_train, scope='language_decoder', reuse=tf.AUTO_REUSE)

        with tf.name_scope('LanguageGenerationLoss'):
            I2_logits, recon_logits = tf.split(logits, 2, axis=0)
            I2_pred, recon_pred = tf.split(pred, 2, axis=0)
            I2_pred_len, recon_pred_len = tf.split(pred_len, 2, axis=0)

            seq_mask = tf.sequence_mask(seq_len + 1, dtype=tf.float32)
            I2_loss = seq2seq.sequence_loss(
                I2_logits, wordset_seq, seq_mask, name='I2_sequence_loss')
            recon_loss = seq2seq.sequence_loss(
                recon_logits, wordset_seq, seq_mask, name='recon_sequence_loss')

            # Accuracy
            I2_token_acc, I2_seq_acc = self.sequence_accuracy(
                I2_pred, I2_pred_len, wordset_seq, seq_len + 1)
            recon_token_acc, recon_seq_acc = self.sequence_accuracy(
                recon_pred, recon_pred_len, wordset_seq, seq_len + 1)

        with tf.name_scope('Summary'):
            gt_string = self.seq2string(wordset_seq, seq_len + 1)
            I2_string = self.seq2string(I2_pred, I2_pred_len)
            recon_string = self.seq2string(recon_pred, recon_pred_len)

        tf.summary.scalar('train-region/I2_loss',
                          I2_loss, collections=['train'])
        tf.summary.scalar('val-region/I2_loss',
                          I2_loss, collections=['val'])
        tf.summary.scalar('train-region/recon_loss',
                          recon_loss, collections=['train'])
        tf.summary.scalar('val-region/recon_loss',
                          recon_loss, collections=['val'])
        tf.summary.scalar('train-region/I2_token_acc',
                          I2_token_acc, collections=['train'])
        tf.summary.scalar('train-region/I2_seq_acc',
                          I2_seq_acc, collections=['train'])
        tf.summary.scalar('val-region/I2_token_acc',
                          I2_token_acc, collections=['val'])
        tf.summary.scalar('val-region/I2_seq_acc',
                          I2_seq_acc, collections=['val'])
        tf.summary.scalar('train-region/recon_token_acc',
                          recon_token_acc, collections=['train'])
        tf.summary.scalar('train-region/recon_seq_acc',
                          recon_seq_acc, collections=['train'])
        tf.summary.scalar('val-region/recon_token_acc',
                          recon_token_acc, collections=['val'])
        tf.summary.scalar('val-region/recon_seq_acc',
                          recon_seq_acc, collections=['val'])

        tf.summary.text('train-region_gt_string',
                        gt_string, collections=['train'])
        tf.summary.text('train-region_I2_string',
                        I2_string, collections=['train'])
        tf.summary.text('train-region_recon_string',
                        recon_string, collections=['train'])
        tf.summary.text('val-region_gt_string',
                        gt_string, collections=['val'])
        tf.summary.text('val-region_I2_string',
                        I2_string, collections=['val'])
        tf.summary.text('val-region_recon_string',
                        recon_string, collections=['val'])
        return I2_loss, recon_loss

    def build(self, is_train=True):
        self.v_loss = 0
        self.l_loss = 0

        if not self.no_object:
            log.info('build object')
            object_v_loss = self.build_object(is_train=is_train)
            self.v_loss += object_v_loss

        if not self.no_region:
            log.info('build region')
            region_v_loss, region_l_loss = self.build_region(is_train=is_train)
            self.v_loss += region_v_loss
            self.l_loss += region_l_loss

        self.loss = self.v_loss + self.l_loss
        tf.summary.scalar('train/loss', self.loss, collections=['train'])
        tf.summary.scalar('val/loss', self.loss, collections=['val'])
        tf.summary.scalar('train/v_loss', self.v_loss, collections=['train'])
        tf.summary.scalar('val/v_loss', self.v_loss, collections=['val'])
        tf.summary.scalar('train/l_loss', self.l_loss, collections=['train'])
        tf.summary.scalar('val/l_loss', self.l_loss, collections=['val'])

        self.report['loss'] = self.loss
