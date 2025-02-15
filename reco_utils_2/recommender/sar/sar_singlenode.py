# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.


import numpy as np
import pandas as pd
import logging
import pickle
from scipy import sparse
import os 
#print(os.getcwd())
from reco_utils.common.python_utils import (
    jaccard,
    lift,
    exponential_decay,
    get_top_k_scored_items,
)
from reco_utils.common import constants


COOCCUR = "cooccurrence"
JACCARD = "jaccard"
LIFT = "lift"
DATA_DIR = '../data/'
logger = logging.getLogger()


class SARSingleNode:
    """Simple Algorithm for Recommendations (SAR) implementation
    
    SAR is a fast scalable adaptive algorithm for personalized recommendations based on user transaction history 
    and items description. The core idea behind SAR is to recommend items like those that a user already has 
    demonstrated an affinity to. It does this by 1) estimating the affinity of users for items, 2) estimating 
    similarity across items, and then 3) combining the estimates to generate a set of recommendations for a given user. 
    """

    def __init__(
        self,
        col_user=constants.DEFAULT_USER_COL,
        col_item=constants.DEFAULT_ITEM_COL,
        col_rating=constants.DEFAULT_RATING_COL,
        col_timestamp=constants.DEFAULT_TIMESTAMP_COL,
        col_prediction=constants.DEFAULT_PREDICTION_COL,
        similarity_type=JACCARD,
        time_decay_coefficient=30,
        time_now=None,
        timedecay_formula=False,
        threshold=1,
        normalize=False,
    ):
        """Initialize model parameters

        Args:
            col_user (str): user column name
            col_item (str): item column name
            col_rating (str): rating column name
            col_timestamp (str): timestamp column name
            col_prediction (str): prediction column name
            similarity_type (str): ['cooccurrence', 'jaccard', 'lift'] option for computing item-item similarity
            time_decay_coefficient (float): number of days till ratings are decayed by 1/2
            time_now (int | None): current time for time decay calculation
            timedecay_formula (bool): flag to apply time decay
            threshold (int): item-item co-occurrences below this threshold will be removed
            normalize (bool): option for normalizing predictions to scale of original ratings
        """

        self.col_rating = col_rating
        self.col_item = col_item
        self.col_user = col_user
        self.col_timestamp = col_timestamp
        self.col_prediction = col_prediction

        if similarity_type not in [COOCCUR, JACCARD, LIFT, "custom"]:
            raise ValueError(
                'Similarity type must be one of ["cooccurrence" | "jaccard" | "lift" | "custom"]'
            )
        self.similarity_type = similarity_type
        self.time_decay_half_life = (
            time_decay_coefficient * 24 * 60 * 60
        )  # convert to seconds
        self.time_decay_flag = timedecay_formula
        self.time_now = time_now
        self.threshold = threshold
        self.user_affinity = None
        self.item_similarity = None
        self.item_frequencies = None

        # threshold - items below this number get set to zero in co-occurrence counts
        if self.threshold <= 0:
            raise ValueError("Threshold cannot be < 1")

        # set flag to capture unity-rating user-affinity matrix for scaling scores
        self.normalize = normalize
        self.col_unity_rating = "_unity_rating"
        self.unity_user_affinity = None

        # column for mapping user / item ids to internal indices
        self.col_item_id = "_indexed_items"
        self.col_user_id = "_indexed_users"

        # obtain all the users and items from both training and test data
        self.n_users = None
        self.n_items = None

        # mapping for item to matrix element
        self.user2index = None
        self.item2index = None

        # the opposite of the above map - map array index to actual string ID
        self.index2item = None

    def compute_affinity_matrix(self, df, rating_col):
        """ Affinity matrix.

        The user-affinity matrix can be constructed by treating the users and items as
        indices in a sparse matrix, and the events as the data. Here, we're treating
        the ratings as the event weights.  We convert between different sparse-matrix
        formats to de-duplicate user-item pairs, otherwise they will get added up.
        
        Args:
            df (pd.DataFrame): Indexed df of users and items
            rating_col (str): Name of column to use for ratings

        Returns:
            sparse.csr: Affinity matrix in Compressed Sparse Row (CSR) format.
        """

        return sparse.coo_matrix(
            (df[rating_col], (df[self.col_user_id], df[self.col_item_id])),
            shape=(self.n_users, self.n_items),
        ).tocsr()

    def compute_time_decay(self, df, decay_column):
        """Compute time decay on provided column.

        Args:
            df (pd.DataFrame): DataFrame of users and items
            decay_column (str): column to decay

        Returns:
            DataFrame: with column decayed
        """

        # if time_now is None use the latest time
        if self.time_now is None:
            self.time_now = df[self.col_timestamp].max()

        # apply time decay to each rating
        df[decay_column] *= exponential_decay(
            value=df[self.col_timestamp],
            max_val=self.time_now,
            half_life=self.time_decay_half_life,
        )

        # group time decayed ratings by user-item and take the sum as the user-item affinity
        return df.groupby([self.col_user, self.col_item]).sum().reset_index()

    def compute_coocurrence_matrix(self, df):
        """ Co-occurrence matrix.

        The co-occurrence matrix is defined as :math:`C = U^T * U`  
        
        where U is the user_affinity matrix with 1's as values (instead of ratings).

        Args:
            df (pd.DataFrame): DataFrame of users and items

        Returns:
            np.array: Co-occurrence matrix
        """

        user_item_hits = sparse.coo_matrix(
            (np.repeat(1, df.shape[0]), (df[self.col_user_id], df[self.col_item_id])),
            shape=(self.n_users, self.n_items),
        ).tocsr()

        item_cooccurrence = user_item_hits.transpose().dot(user_item_hits)
        item_cooccurrence = item_cooccurrence.multiply(
            item_cooccurrence >= self.threshold
        )

        return item_cooccurrence.astype(df[self.col_rating].dtype)

    def set_index(self, df):
        """Generate continuous indices for users and items to reduce memory usage.

        Args:
            df (pd.DataFrame): dataframe with user and item ids
        """

        # generate a map of continuous index values to items
        print('items in df', df[self.col_item].size)
        self.index2item = dict(enumerate(df[self.col_item].unique()))
        print('index to item len', len(self.index2item))

        # invert the mapping from above
        self.item2index = {v: k for k, v in self.index2item.items()}
        print('item to index len', len(self.item2index))

        # create mapping of users to continuous indices
        self.user2index = {x[1]: x[0] for x in enumerate(df[self.col_user].unique())}

        # set values for the total count of users and items
        self.n_users = len(self.user2index)
        self.n_items = len(self.index2item)

    def fit(self, df, features, col_itemid, col_weights, demo=False):
        """Main fit method for SAR.

        Args:
            df (pd.DataFrame): User item rating dataframe
            features (pd.DataFrame): item feature dataframe
            col_itemid (string): name of the item id column of the feature dataframe
            col_weights (dictionary): mapping feature column names to their (weight, similarity_function) 
            in the similarity metric required to contain key 'ratings' with the weight of the similarity based on user ratings
            col_weights of features that are not 'ratings' should sum to 1 
        """
        num_items = len(features)
        load = False
        experiment_path = DATA_DIR + 'experiment/'
        demo_path = DATA_DIR + 'demo/'
        path = experiment_path
        if os.path.exists(experiment_path+'item_feature_similarity_{}.npy'.format(num_items)):
            load = True
            
        if demo:
            load = True
            path = demo_path

        if load:
            self.load_file(path, num_items, df) #set features_sim_matrix, index2item, item2index
        
        elif self.index2item is None:
            # generate continuous indices if this hasn't been done
            self.set_index(df)

        logger.info("Collecting user affinity matrix")
        if not np.issubdtype(df[self.col_rating].dtype, np.number):
            raise TypeError("Rating column data type must be numeric")

        # copy the DataFrame to avoid modification of the input
        select_columns = [self.col_user, self.col_item, self.col_rating]
        if self.time_decay_flag:
            select_columns += [self.col_timestamp]
        temp_df = df[select_columns].copy()

        if self.time_decay_flag:
            logger.info("Calculating time-decayed affinities")
            temp_df = self.compute_time_decay(df=temp_df, decay_column=self.col_rating)
        else:
            # without time decay use the latest user-item rating in the dataset as the affinity score
            logger.info("De-duplicating the user-item counts")
            temp_df = temp_df.drop_duplicates(
                [self.col_user, self.col_item], keep="last"
            )

        logger.info("Creating index columns")
        # add mapping of user and item ids to indices
        temp_df.loc[:, self.col_item_id] = temp_df[self.col_item].map(self.item2index)
        temp_df.loc[:, self.col_user_id] = temp_df[self.col_user].map(self.user2index)

        if self.normalize:
            logger.info("Calculating normalization factors")
            temp_df[self.col_unity_rating] = 1.0
            if self.time_decay_flag:
                temp_df = self.compute_time_decay(df=temp_df, decay_column=self.col_unity_rating)
            self.unity_user_affinity = self.compute_affinity_matrix(df=temp_df, rating_col=self.col_unity_rating)

        # affinity matrix
        logger.info("Building user affinity sparse matrix")
        self.user_affinity = self.compute_affinity_matrix(df=temp_df, rating_col=self.col_rating)
        
        # calculate item co-occurrence
        logger.info("Calculating item co-occurrence")
        item_cooccurrence = self.compute_coocurrence_matrix(df=temp_df)

        # free up some space
        del temp_df

        self.item_frequencies = item_cooccurrence.diagonal()

        logger.info("Calculating item similarity")
        if self.similarity_type is COOCCUR:
            logger.info("Using co-occurrence based similarity")
            self.item_similarity = item_cooccurrence
        elif self.similarity_type is JACCARD:
            logger.info("Using jaccard based similarity")
            self.item_similarity = jaccard(item_cooccurrence).astype(
                df[self.col_rating].dtype
            )
        elif self.similarity_type is LIFT:
            logger.info("Using lift based similarity")
            self.item_similarity = lift(item_cooccurrence).astype(
                df[self.col_rating].dtype
            )
        elif self.similarity_type is "custom":
            self.item_similarity = col_weights['ratings'] * jaccard(item_cooccurrence).astype(df[self.col_rating].dtype)
            if not load:
                self.features_sim_matrix = self.compute_feature_sim_matrix(col_weights, features, col_itemid)
                self.save_to_file(path)
            #!!! assuming self.features_sim_matrix has scale 1 (i.e. col_weights[features] all sum to 1)
            self.item_similarity += (1-col_weights['ratings'])*self.features_sim_matrix   
        else:
            raise ValueError("Unknown similarity type: {}".format(self.similarity_type))

        # free up some space
        del item_cooccurrence

        logger.info("Done training")
    
    def compute_feature_sim_matrix(self, col_weights, features, col_itemid): 
        num_items = len(features)
        features_sim_matrix = np.zeros((num_items, num_items))
        for col_feature in col_weights:
            if col_feature == 'ratings':
               continue
            weight, similarity_function = col_weights[col_feature]
            features_sim_matrix += weight * self.make_sim_matrix(features, col_itemid, col_feature, similarity_function) 
        return features_sim_matrix


    def make_sim_matrix(self, df_features, col_id, col_sim, f, load=True):
        num_items = len(df_features)
        sim_matrix = np.empty((num_items, num_items), dtype=np.float16)
        for i in range(0, num_items):
            for j in range(0, num_items): # case j > i redundant, could stop earlier
                index_i = self.item2index[df_features.loc[i][col_id]]
                index_j = self.item2index[df_features.loc[j][col_id]]
                sim_matrix[index_i,index_j] =  f(df_features.loc[i][col_sim], df_features.loc[j][col_sim])
        return sim_matrix

    def load_file(self, path, num_items, df_ratings):
        self.features_sim_matrix = np.load(path+'item_feature_similarity_{}.npy'.format(num_items))
        with open(path+'index2item_{}.obj'.format(num_items), 'rb') as file_index2item:
            self.index2item = pickle.load(file_index2item)

        with open(path+'item2index_{}.obj'.format(num_items), 'rb') as file_item2index:
            self.item2index = pickle.load(file_item2index)
           # create mapping of users to continuous indices
            self.user2index = {x[1]: x[0] for x in enumerate(df_ratings[self.col_user].unique())}

           # set values for the total count of users and items
            self.n_users = self.n_items = len(self.user2index)
            self.n_items = len(self.index2item)
       
        
    def save_to_file(self, path):
        np.save(path+'item_feature_similarity_{}.npy'.format(self.n_items), self.features_sim_matrix)

        with open(path+'item2index_{}.obj'.format(self.n_items), 'wb') as filehandler:
            pickle.dump(self.item2index, filehandler)

        with open(path+'index2item_{}.obj'.format(self.n_items), 'wb')  as filehandler:
            pickle.dump(self.index2item, filehandler)

    def score(self, test, remove_seen=False, normalize=False):
        """Score all items for test users.

        Args:
            test (pd.DataFrame): user to test
            remove_seen (bool): flag to remove items seen in training from recommendation
            normalize (bool): flag to normalize scores to be in the same scale as the original ratings
 
        Returns:
            np.ndarray: Value of interest of all items for the users.
        """

        # get user / item indices from test set
        user_ids = test[self.col_user].drop_duplicates().map(self.user2index).values
        if any(np.isnan(user_ids)):
            raise ValueError("SAR cannot score users that are not in the training set")

        # calculate raw scores with a matrix multiplication
        logger.info("Calculating recommendation scores")
        test_scores = self.user_affinity[user_ids, :].dot(self.item_similarity)

        # ensure we're working with a dense ndarray
        if isinstance(test_scores, sparse.spmatrix):
            test_scores = test_scores.toarray()

        # remove items in the train set so recommended items are always novel
        if remove_seen:
            logger.info("Removing seen items")
            test_scores += self.user_affinity[user_ids, :] * -np.inf

        if normalize:
            if self.unity_user_affinity is None:
                raise ValueError('Cannot use normalize flag during scoring if it was not set at model instantiation')
            else:
                test_scores = np.array(
                    np.divide(
                        test_scores,
                        self.unity_user_affinity[user_ids, :].dot(self.item_similarity)
                    )
                )
                test_scores = np.where(np.isnan(test_scores), -np.inf, test_scores)

        return test_scores

    def get_popularity_based_topk(self, top_k=10, sort_top_k=True):
        """Get top K most frequently occurring items across all users.

        Args:
            top_k (int): number of top items to recommend.
            sort_top_k (bool): flag to sort top k results.

        Returns:
            pd.DataFrame: top k most popular items.
        """

        test_scores = np.array([self.item_frequencies])

        logger.info("Getting top K")
        top_items, top_scores = get_top_k_scored_items(
            scores=test_scores, top_k=top_k, sort_top_k=sort_top_k
        )

        return pd.DataFrame(
            {
                self.col_item: [self.index2item[item] for item in top_items.flatten()],
                self.col_prediction: top_scores.flatten(),
            }
        )

    def get_item_based_topk(self, items, top_k=10, sort_top_k=True):
        """Get top K similar items to provided seed items based on similarity metric defined.
        This method will take a set of items and use them to recommend the most similar items to that set
        based on the similarity matrix fit during training.
        This allows recommendations for cold-users (unseen during training), note - the model is not updated.

        The following options are possible based on information provided in the items input:
        1. Single user or seed of items: only item column (ratings are assumed to be 1)
        2. Single user or seed of items w/ ratings: item column and rating column
        3. Separate users or seeds of items: item and user column (user ids are only used to separate item sets)
        4. Separate users or seeds of items with ratings: item, user and rating columns provided

        Args:
            items (pd.DataFrame): DataFrame with item, user (optional), and rating (optional) columns
            top_k (int): number of top items to recommend
            sort_top_k (bool): flag to sort top k results

        Returns:
            pd.DataFrame: sorted top k recommendation items
        """

        # convert item ids to indices
        item_ids = items[self.col_item].map(self.item2index)

        # if no ratings were provided assume they are all 1
        if self.col_rating in items.columns:
            ratings = items[self.col_rating]
        else:
            ratings = pd.Series(np.ones_like(item_ids))

        # create local map of user ids
        if self.col_user in items.columns:
            test_users = items[self.col_user]
            user2index = {x[1]: x[0] for x in enumerate(items[self.col_user].unique())}
            user_ids = test_users.map(user2index)
        else:
            # if no user column exists assume all entries are for a single user
            test_users = pd.Series(np.zeros_like(item_ids))
            user_ids = test_users
        n_users = user_ids.drop_duplicates().shape[0]

        # generate pseudo user affinity using seed items
        pseudo_affinity = sparse.coo_matrix(
            (ratings, (user_ids, item_ids)), shape=(n_users, self.n_items)
        ).tocsr()

        # calculate raw scores with a matrix multiplication
        test_scores = pseudo_affinity.dot(self.item_similarity)

        # remove items in the seed set so recommended items are novel
        test_scores[user_ids, item_ids] = -np.inf

        top_items, top_scores = get_top_k_scored_items(
            scores=test_scores, top_k=top_k, sort_top_k=sort_top_k
        )

        df = pd.DataFrame(
            {
                self.col_user: np.repeat(
                    test_users.drop_duplicates().values, top_items.shape[1]
                ),
                self.col_item: [self.index2item[item] for item in top_items.flatten()],
                self.col_prediction: top_scores.flatten(),
            }
        )

        # drop invalid items
        return df.replace(-np.inf, np.nan).dropna()

    def recommend_k_items(
        self, test, top_k=10, sort_top_k=True, remove_seen=False, normalize=False
    ):
        """Recommend top K items for all users which are in the test set

        Args:
            test (pd.DataFrame): users to test
            top_k (int): number of top items to recommend
            sort_top_k (bool): flag to sort top k results
            remove_seen (bool): flag to remove items seen in training from recommendation

        Returns:
            pd.DataFrame: top k recommendation items for each user
        """

        test_scores = self.score(test, remove_seen=remove_seen, normalize=normalize)

        top_items, top_scores = get_top_k_scored_items(
            scores=test_scores, top_k=top_k, sort_top_k=sort_top_k
        )

        df = pd.DataFrame(
            {
                self.col_user: np.repeat(
                    test[self.col_user].drop_duplicates().values, top_items.shape[1]
                ),
                self.col_item: [self.index2item[item] for item in top_items.flatten()],
                self.col_prediction: top_scores.flatten(),
            }
        )

        # drop invalid items
        return df.replace(-np.inf, np.nan).dropna()

    def predict(self, test):
        """Output SAR scores for only the users-items pairs which are in the test set
        
        Args:
            test (pd.DataFrame): DataFrame that contains users and items to test

        Returns:
            pd.DataFrame: DataFrame contains the prediction results
        """

        test_scores = self.score(test)
        user_ids = test[self.col_user].map(self.user2index).values

        # create mapping of new items to zeros
        item_ids = test[self.col_item].map(self.item2index).values
        nans = np.isnan(item_ids)
        if any(nans):
            logger.warning(
                "Items found in test not seen during training, new items will have score of 0"
            )
            test_scores = np.append(test_scores, np.zeros((self.n_users, 1)), axis=1)
            item_ids[nans] = self.n_items
            item_ids = item_ids.astype("int64")

        df = pd.DataFrame(
            {
                self.col_user: test[self.col_user].values,
                self.col_item: test[self.col_item].values,
                self.col_prediction: test_scores[user_ids, item_ids],
            }
        )
        return df


#def jaccard_simple(a, b):
#    return len(set(a).intersection(set(b)))/len(set(a).union(set(b)))




