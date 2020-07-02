"""

Reference: Paul Covington et al.  "Deep Neural Networks for YouTube Recommendations"
           (https://static.googleusercontent.com/media/research.google.com/zh-CN//pubs/archive/45530.pdf)

author: massquantity

"""
import time
from itertools import islice
import numpy as np
import tensorflow as tf
from tensorflow.python.keras.initializers import (
    zeros as tf_zeros,
    truncated_normal as tf_truncated_normal
)
from .base import Base, TfMixin
from ..evaluate.evaluate import EvalMixin
from ..utils.tf_ops import (
    reg_config,
    dropout_config,
    dense_nn,
    lr_decay_config
)
from ..data.data_generator import DataGenSequence
from ..data.sequence import user_last_interacted
from ..utils.sampling import NegativeSampling
from ..utils.tf_ops import sparse_tensor_interaction
from ..utils.colorize import colorize
from ..utils.timing import time_block
from ..utils.unique_features import (
    get_predict_indices_and_values,
    get_recommend_indices_and_values
)


class YouTubeRanking(Base, TfMixin, EvalMixin):
    """
    The model implemented mainly corresponds to the ranking phase
    based on the original paper.
    """
    def __init__(self, task="ranking", data_info=None, embed_size=16,
                 n_epochs=20, lr=0.01, lr_decay=False, reg=None,
                 batch_size=256, num_neg=1, use_bn=True, dropout_rate=None,
                 hidden_units="128,64,32", recent_num=10, random_num=None,
                 seed=42, lower_upper_bound=None, tf_sess_config=None):

        Base.__init__(self, task, data_info, lower_upper_bound)
        TfMixin.__init__(self, tf_sess_config)
        EvalMixin.__init__(self, task)

        self.task = task
        self.data_info = data_info
        self.embed_size = embed_size
        self.n_epochs = n_epochs
        self.lr = lr
        self.lr_decay = lr_decay
        self.reg = reg_config(reg)
        self.batch_size = batch_size
        self.num_neg = num_neg
        self.use_bn = use_bn
        self.dropout_rate = dropout_config(dropout_rate)
        self.hidden_units = list(map(int, hidden_units.split(",")))
        self.n_users = data_info.n_users
        self.n_items = data_info.n_items
        self.global_mean = data_info.global_mean
        self.default_prediction = data_info.global_mean if (
                task == "rating") else 0.0
        (self.interaction_mode,
         self.interaction_num) = self._check_interaction_mode(
            recent_num, random_num)
        self.seed = seed
        self.user_consumed = None
        self.sparse = self._decide_sparse_indices(data_info)
        self.dense = self._decide_dense_values(data_info)
        if self.sparse:
            self.sparse_feature_size = self._sparse_feat_size(data_info)
            self.sparse_field_size = self._sparse_field_size(data_info)
        if self.dense:
            self.dense_field_size = self._dense_field_size(data_info)
        self.user_last_interacted = None
        self.last_interacted_len = None

    def _build_model(self):
        tf.set_random_seed(self.seed)
        self.user_indices = tf.placeholder(tf.int32, shape=[None])
        self.item_indices = tf.placeholder(tf.int32, shape=[None])
        self.user_interacted_indices = tf.placeholder(
            tf.int32, shape=[None, self.interaction_num])
        self.user_interacted_len = tf.placeholder(tf.float32, shape=[None])
        self.labels = tf.placeholder(tf.float32, shape=[None])
        self.is_training = tf.placeholder_with_default(True, shape=[])
        self.concat_embed = []

        user_features = tf.get_variable(
            name="user_features",
            shape=[self.n_users, self.embed_size],
            initializer=tf_truncated_normal(0.0, 0.01),
            regularizer=self.reg)
        item_features = tf.get_variable(
            name="item_features",
            shape=[self.n_items + 1, self.embed_size],
            initializer=tf_truncated_normal(0.0, 0.01),
            regularizer=self.reg)
        user_embed = tf.nn.embedding_lookup(user_features, self.user_indices)
        item_embed = tf.nn.embedding_lookup(item_features, self.item_indices)

        zero_padding_op = tf.scatter_update(
            item_features, self.n_items,
            tf.zeros([self.embed_size], dtype=tf.float32)
        )
        with tf.control_dependencies([zero_padding_op]):
            multi_item_embed = tf.nn.embedding_lookup(
                item_features, self.user_interacted_indices)  # B * seq * K
        pooled_embed = tf.div_no_nan(
            tf.reduce_sum(multi_item_embed, axis=1),
            tf.expand_dims(tf.sqrt(self.user_interacted_len), axis=1))
        self.concat_embed.extend([user_embed, item_embed, pooled_embed])

        if self.sparse:
            self._build_sparse()
        if self.dense:
            self._build_dense()

        concat_embed = tf.concat(self.concat_embed, axis=1)
        mlp_layer = dense_nn(concat_embed,
                             self.hidden_units,
                             use_bn=self.use_bn,
                             dropout_rate=self.dropout_rate,
                             is_training=self.is_training)
        self.output = tf.reshape(
            tf.layers.dense(inputs=mlp_layer, units=1), [-1])

    def _build_sparse(self):
        self.sparse_indices = tf.placeholder(
            tf.int32, shape=[None, self.sparse_field_size])
        sparse_features = tf.get_variable(
            name="sparse_features",
            shape=[self.sparse_feature_size, self.embed_size],
            initializer=tf_truncated_normal(0.0, 0.01),
            regularizer=self.reg)

        sparse_embed = tf.nn.embedding_lookup(
            sparse_features, self.sparse_indices)
        sparse_embed = tf.reshape(
            sparse_embed, [-1, self.sparse_field_size * self.embed_size])
        self.concat_embed.append(sparse_embed)

    def _build_dense(self):
        self.dense_indices = tf.placeholder(
            tf.int32, shape=[None, self.dense_field_size])
        self.dense_values = tf.placeholder(
            tf.float32, shape=[None, self.dense_field_size])

        dense_features = tf.get_variable(
            name="dense_features",
            shape=[self.dense_field_size, self.embed_size],
            initializer=tf_truncated_normal(0.0, 0.01),
            regularizer=self.reg)
        dense_embed = tf.nn.embedding_lookup(
            dense_features, self.dense_indices)
        dense_values = tf.reshape(
            self.dense_values, [-1, self.dense_field_size, 1])
        dense_embed = tf.multiply(dense_embed, dense_values)
        dense_embed = tf.reshape(
            dense_embed, [-1, self.dense_field_size * self.embed_size])
        self.concat_embed.append(dense_embed)

    def _build_train_ops(self, global_steps=None):
        self.loss = tf.reduce_mean(
            tf.nn.sigmoid_cross_entropy_with_logits(labels=self.labels,
                                                    logits=self.output)
        )

        if self.reg is not None:
            reg_keys = tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES)
            total_loss = self.loss + tf.add_n(reg_keys)
        else:
            total_loss = self.loss

        optimizer = tf.train.AdamOptimizer(self.lr)
        optimizer_op = optimizer.minimize(total_loss, global_step=global_steps)
        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        self.training_op = tf.group([optimizer_op, update_ops])
        self.sess.run(tf.global_variables_initializer())

    def fit(self, train_data, verbose=1, shuffle=True, sample_rate=None,
            recent_num=None, eval_data=None, metrics=None, **kwargs):

        start_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
        print(f"training start time: {colorize(start_time, 'magenta')}")
        self.user_consumed = train_data.user_consumed
        if self.lr_decay:
            n_batches = int(len(train_data) / self.batch_size)
            self.lr, global_steps = lr_decay_config(self.lr, n_batches,
                                                    **kwargs)
        else:
            global_steps = None

        self._build_model()
        self._build_train_ops(global_steps)

        data_generator = DataGenSequence(train_data, self.sparse, self.dense,
                                         mode=self.interaction_mode,
                                         num=self.interaction_num,
                                         n_items=self.n_items)
        for epoch in range(1, self.n_epochs + 1):
            if self.lr_decay:
                print(f"With lr_decay, epoch {epoch} learning rate: "
                      f"{self.sess.run(self.lr)}")
            with time_block(f"Epoch {epoch}", verbose):
                train_total_loss = []
                for ui, u_len, user, item, label, si, di, dv in data_generator(
                        shuffle, self.batch_size):
                    feed_dict = self._youtube_feed_dicts(
                        ui, u_len, user, item, label, si, di, dv, True)
                    train_loss, _ = self.sess.run(
                        [self.loss, self.training_op], feed_dict)
                    train_total_loss.append(train_loss)

            if verbose > 1:
                train_loss_str = "train_loss: " + str(
                    round(np.mean(train_total_loss), 4)
                )
                print(f"\t {colorize(train_loss_str, 'green')}")
                # for evaluation
                self._set_last_interacted()
                self.print_metrics(eval_data=eval_data, metrics=metrics)
                print("=" * 30)

        # for prediction and recommendation
        self._set_last_interacted()

    def predict(self, user, item):
        user = np.asarray(
            [user]) if isinstance(user, int) else np.asarray(user)
        item = np.asarray(
            [item]) if isinstance(item, int) else np.asarray(item)

        unknown_num, unknown_index, user, item = self._check_unknown(
            user, item)

        (user_indices,
         item_indices,
         sparse_indices,
         dense_indices,
         dense_values) = get_predict_indices_and_values(
            self.data_info, user, item, self.n_items, self.sparse, self.dense)
        feed_dict = self._youtube_feed_dicts(
            self.user_last_interacted[user], self.last_interacted_len[user],
            user_indices, item_indices, None, sparse_indices, dense_indices,
            dense_values, False)

        preds = self.sess.run(self.output, feed_dict)
        preds = 1 / (1 + np.exp(-preds))
        if unknown_num > 0:
            preds[unknown_index] = self.default_prediction

        return preds[0] if len(user) == 1 else preds

    def recommend_user(self, user, n_rec, **kwargs):
        user = self._check_unknown_user(user)
        if not user:
            return   # popular ?

        (user_indices,
         item_indices,
         sparse_indices,
         dense_indices,
         dense_values) = get_recommend_indices_and_values(
            self.data_info, user, self.n_items, self.sparse, self.dense)
        u_last_interacted = np.tile(self.user_last_interacted[user],
                                    (self.n_items, 1))
        u_interacted_len = np.repeat(self.last_interacted_len[user],
                                     self.n_items)
        feed_dict = self._youtube_feed_dicts(
            u_last_interacted, u_interacted_len,
            user_indices, item_indices, None, sparse_indices, dense_indices,
            dense_values, False)

        recos = self.sess.run(self.output, feed_dict)
        recos = 1 / (1 + np.exp(-recos))
        consumed = self.user_consumed[user]
        count = n_rec + len(consumed)
        ids = np.argpartition(recos, -count)[-count:]
        rank = sorted(zip(ids, recos[ids]), key=lambda x: -x[1])
        return list(
            islice(
                (rec for rec in rank if rec[0] not in consumed), n_rec
            )
        )

    def _youtube_feed_dicts(self, user_interacted, u_len, user, item, label,
                            sparse_indices, dense_indices, dense_values,
                            is_training):
        feed_dict = {self.user_interacted_indices: user_interacted,
                     self.user_interacted_len: u_len,
                     self.user_indices: user,
                     self.item_indices: item,
                     self.is_training: is_training}
        if self.sparse:
            feed_dict.update({self.sparse_indices: sparse_indices})
        if self.dense:
            feed_dict.update({self.dense_indices: dense_indices,
                              self.dense_values: dense_values})
        if label is not None:
            feed_dict.update({self.labels: label})
        return feed_dict

    def user_indices_generator(self, data):
        total_user_indices = data.user_indices
        user_indices_length = len(total_user_indices)
        for k in range(0, user_indices_length, self.batch_size):
            user_indices = total_user_indices[k: k+self.batch_size]
            user_indices = np.repeat(user_indices, self.num_neg+1)
            yield user_indices

    def _set_last_interacted(self):
        user_indices = np.arange(self.n_users)
        (self.user_last_interacted,
         self.last_interacted_len) = user_last_interacted(
            user_indices, self.user_consumed, self.n_items,
            self.interaction_num)

    def _check_interaction_mode(self, recent_num, random_num):
        if recent_num is not None:
            assert isinstance(recent_num, int), "recent_num must be integer"
            mode = "recent"
            num = recent_num
        elif random_num is not None:
            assert isinstance(random_num, int), "random_num must be integer"
            mode = "random"
            num = random_num
        else:
            mode = "recent"
            num = 10  # by default choose 10 recent interactions
        return mode, num

