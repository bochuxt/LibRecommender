import time
from collections import defaultdict
from multiprocessing import Pool
import numpy as np


class NegativeSamplingFeat:
    def __init__(self, dataset, data_info, num_neg, batch_size=64):
        self.dataset = dataset
        self.data_info = data_info
        self.num_neg = num_neg
        self.batch_size = batch_size

    def __call__(self, seed=42, dense=True, random=True):
        np.random.seed(seed)
        total_length = len(self.dataset) * (self.num_neg + 1)
        random_mask = np.random.permutation(range(total_length))

        label_sampled = self._label_negative_sampling()
        if random:
            label_sampled = label_sampled[random_mask]

        item_indices_sampled = self.sample_items()
        sparse_sampled = self._sparse_negative_sampling(item_indices_sampled)
        if random:
            sparse_sampled = sparse_sampled[random_mask]
        if dense:
            dense_sampled = self._dense_negative_sampling(item_indices_sampled)
            if random:
                dense_sampled = dense_sampled[random_mask]
            return sparse_sampled, dense_sampled, label_sampled
        return sparse_sampled, label_sampled

    def sample_items(self):
        item_indices_sampled = list()
        n_items = self.data_info.n_items
        # sample negative items for every user
        t0 = time.time()
        for u, i in zip(self.dataset.user_indices, self.dataset.item_indices):
            item_indices_sampled.append(i)
            for _ in range(self.num_neg):
                item_neg = np.random.randint(0, n_items)
                while item_neg in self.dataset.train_user_consumed[u]:
                    item_neg = np.random.randint(0, n_items)
                item_indices_sampled.append(item_neg)
        print("neg: ", time.time() - t0)
        return np.array(item_indices_sampled)

    def _sparse_negative_sampling(self, item_indices_sampled):
        user_sparse_col = self.data_info.user_sparse_col.index
        user_sparse_indices = np.take(self.dataset.sparse_indices, user_sparse_col, axis=1)
        user_sparse_sampled = np.repeat(user_sparse_indices, self.num_neg + 1, axis=0)

        item_sparse_col = self.data_info.item_sparse_col.index
        item_sparse_sampled = self.data_info.item_sparse_unique[item_indices_sampled]

        assert len(user_sparse_sampled) == len(item_sparse_sampled), \
            "num of user sampled must equal to num of item sampled"
        # keep column names in original order
        orig_cols = user_sparse_col + item_sparse_col
        col_reindex = np.arange(len(orig_cols))[np.argsort(orig_cols)]
        return np.concatenate([user_sparse_sampled, item_sparse_sampled], axis=-1)[:, col_reindex]

    def _dense_negative_sampling(self, item_indices_sampled):
        user_dense_col = self.data_info.user_dense_col.index
        user_dense_values = np.take(self.dataset.dense_values, user_dense_col, axis=1)
        user_dense_sampled = np.repeat(user_dense_values, self.num_neg + 1, axis=0)

        item_dense_col = self.data_info.item_dense_col.index
        item_dense_sampled = self.data_info.item_dense_unique[item_indices_sampled]

        assert len(user_dense_sampled) == len(item_dense_sampled), \
            "num of user sampled must equal to num of item sampled"
        # keep column names in original order
        orig_cols = user_dense_col + item_dense_col
        col_reindex = np.arange(len(orig_cols))[np.argsort(orig_cols)]
        return np.concatenate([user_dense_sampled, item_dense_sampled], axis=-1)[:, col_reindex]

    def _label_negative_sampling(self):
        factor = self.num_neg + 1
        total_length = len(self.dataset) * factor
        labels = np.zeros(total_length, dtype=np.float32)
        labels[::factor] = 1.0
        return labels

    def next_batch(self):
        if self.pre_sampling:
            end = min(len(self.dataset.train_indices_implicit), (self.i + 1) * self.batch_size)
            batch_feat_indices = self.dataset.train_indices_implicit[self.i * self.batch_size: end]
            batch_feat_values = self.dataset.train_values_implicit[self.i * self.batch_size: end]
            batch_labels = self.dataset.train_labels_implicit[self.i * self.batch_size: end]
            self.i += 1
            return batch_feat_indices, batch_feat_values, batch_labels
        else:
            batch_size = int(self.batch_size / (self.num_neg + 1))  # positive samples in one batch
            end = min(len(self.dataset.train_feat_indices), (self.i + 1) * batch_size)
            batch_feat_indices = self.dataset.train_feat_indices[self.i * batch_size: end]
            batch_feat_values = self.dataset.train_feat_values[self.i * batch_size: end]
            batch_feat_labels = self.dataset.train_labels[self.i * batch_size: end]

            indices, values, labels = [], [], []
            for i, sample in enumerate(batch_feat_indices):
                ss = sample.tolist()
                user = ss[-2] - self.dataset.user_offset
                indices.append(batch_feat_indices[i])
                values.append(batch_feat_values[i])
                labels.append(batch_feat_labels[i])
                for _ in range(self.num_neg):
                    item_neg = np.random.randint(0, self.dataset.n_items)
                    while item_neg in self.dataset.train_user[user]:
                        item_neg = np.random.randint(0, self.dataset.n_items)
                    item_neg += (self.dataset.user_offset + self.dataset.n_users)  # item offset
                    ss[-1] = item_neg

                    if self.item_feat:
                        dt = self.neg_indices_dict[item_neg]
                        for col, orig_col in enumerate(self.dataset.item_feature_cols):
                            ss[orig_col] = dt[col]

                    indices.append(ss)
                    vv = batch_feat_values[i]
                    if self.item_feat:
                        dv = self.neg_values_dict[item_neg]
                        for col, orig_col in enumerate(self.dataset.item_numerical_cols):
                            vv[orig_col] = dv[col]
                    values.append(vv)
                    labels.append(0.0)

            self.i += 1
            random_mask = np.random.permutation(len(indices))
            return np.array(indices)[random_mask], np.array(values)[random_mask], np.array(labels)[random_mask]






