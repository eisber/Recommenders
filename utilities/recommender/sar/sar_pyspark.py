"""
Reference implementation of SAR in pySpark using Spark-SQL and some dataframe operations.
This is supposed to be a super-performant implementation of SAR on Spark using pySpark.
"""

import pyspark.sql.functions as F

SIM_COOCCUR = "cooccurrence"
SIM_JACCARD = "jaccard"
SIM_LIFT = "lift"

class SARpySparkReference():
    """SAR reference implementation"""

    def __init__(self, spark, remove_seen=True, col_user='userID', col_item='itemID',
                 col_rating='rating', col_timestamp='timestamp',
                 similarity_type='jaccard',
                 time_decay_coefficient=False, time_now=None,
                 timedecay_formula=False, threshold=1, debug = False):

        self.col_rating = col_rating
        self.col_item = col_item
        self.col_user = col_user
        # default values for all SAR algos
        self.col_timestamp = col_timestamp

        self.remove_seen = remove_seen

        # time of item-item similarity
        self.similarity_type = similarity_type
        # denominator in time decay. Zero makes time decay irrelevant
        self.time_decay_coefficient = time_decay_coefficient
        # toggle the computation of time decay group by formula
        self.timedecay_formula = timedecay_formula
        # current time for time decay calculation
        self.time_now = time_now
        # cooccurrence matrix threshold
        self.threshold = threshold
        # debug the code
        self.debug = debug

        # spark context
        self.spark = spark

        # we use these handles for unit tests
        self.item_similarity = None
        self.affinity = None

        # threshold - items below this number get set to zero in coocurrence counts
        assert self.threshold > 0

        # affinity scores for the recommendation
        self.scores = None

    def fit(self, df):
        """Main fit method for SAR. Expects the dataframes to have row_id, col_id columns which are indexes,
        i.e. contain the sequential integer index of the original alphanumeric user and item IDs.
        Dataframe also contains rating and timestamp as floats; timestamp is in seconds since Epoch by default.

        Arguments:
            df (pySpark.DataFrame): input dataframe which contains the index of users and items. """

        # record the training dataframe
        self.df = df

        if self.timedecay_formula:
           # WARNING: previously we would take the last value in training dataframe and set it
           # as a matrix U element
           # for each user-item pair. Now with time decay, we compute a sum over ratings given
           # by a user in the case
           # when T=np.inf, so user gets a cumulative sum of ratings for a particular item and
           # not the last rating.
           # Time Decay
           # do a group by on user item pairs and apply the formula for time decay there
           # Time T parameter is in days and input time is in seconds
           # so we do dt/60/(T*24*60)=dt/(T*24*3600)
           # the folling is the query which we want to run
           df.createOrReplaceTempView("df_train_input")

           query = """
           SELECT
            {col_user}, {col_item}, 
            SUM({col_rating} * EXP(-log(2) * (latest_timestamp - CAST({col_timestamp} AS long)) / ({time_decay_coefficient} * 3600 * 24))) as {col_rating}
           FROM df_train_input,
                (SELECT CAST(MAX({col_timestamp}) AS long) latest_timestamp FROM df_train_input)
           GROUP BY {col_user}, {col_item} 
           CLUSTER BY {col_user} 
            """.format(col_rating = self.col_rating,
                       col_item = self.col_item,
                       col_user = self.col_user,
                       time_now = self.time_now,
                       col_timestamp = self.col_timestamp,
                       time_decay_coefficient = self.time_decay_coefficient)

           df = self.spark.sql(query)

        # record affinity scores
        self.affinity = df

        df.createOrReplaceTempView("df_train")

        # filter out cooccurence counts which are below threshold
        query = """
        SELECT A.{col_item} i1, B.{col_item} i2, count(*) value
        FROM   df_train A INNER JOIN df_train B
               ON A.{col_user} = B.{col_user} AND A.{col_item} <= b.{col_item}  
        GROUP  BY A.{col_item}, B.{col_item}
        HAVING count(*) >= {threshold}
        CLUSTER BY i1, i2
        """.format(col_item = self.col_item, col_user = self.col_user, threshold = self.threshold)
        
        item_cooccurrence = self.spark.sql(query)
        item_cooccurrence.write.mode("overwrite").saveAsTable("item_cooccurrence")
        self.spark.table("item_cooccurrence").cache()
 
        similarity_type = (SIM_COOCCUR if self.similarity_type is None
                           else self.similarity_type)

        # compute the diagonal used later for Jaccard and Lift
        if similarity_type == SIM_LIFT or similarity_type == SIM_JACCARD:
            item_marginal = self.spark.sql("SELECT i1 i, value AS margin FROM item_cooccurrence WHERE i1 = i2")
            item_marginal.createOrReplaceTempView("item_marginal")

        if similarity_type == SIM_COOCCUR:
            self.item_similarity = item_cooccurrence
        elif similarity_type == SIM_JACCARD:
            query = """
            SELECT i1, i2, value / (M1.margin + M2.margin - value) AS value
            FROM item_cooccurrence A 
                INNER JOIN item_marginal M1 ON A.i1 = M1.i 
                INNER JOIN item_marginal M2 ON A.i2 = M2.i
            CLUSTER BY i1, i2
            """
            # log.info("Running query -- " + query)
            self.item_similarity = self.spark.sql(query)
        elif similarity_type == SIM_LIFT:
            query = """
            SELECT i1, i2, value / (M1.margin * M2.margin) AS value
            FROM item_cooccurrence A 
                INNER JOIN item_marginal M1 ON A.i1 = M1.i 
                INNER JOIN item_marginal M2 ON A.i2 = M2.i
            CLUSTER BY i1, i2
            """
            #log.info("Running query -- " + query)
            self.item_similarity = self.spark.sql(query)
        else:
            raise ValueError("Unknown similarity type: {0}".format(similarity_type))

        self.item_similarity.write.mode("overwrite").saveAsTable("item_similarity")
        self.spark.table("item_similarity").cache()
        
        # free memory
        self.spark.sql("DROP TABLE item_cooccurrence") # TODO: if not exists
        self.item_similarity = self.spark.table("item_similarity")

    def recommend_k_items(self, test, top_k=10, output_pandas=False, **kwargs):
        """Recommend top K items for all users which are in the test set.

        Args:
            test: indexed test Spark dataframe
            top_k: top n items to return
            output_pandas: specify whether to convert the output dataframe to Pandas.
            **kwargs:
        """

        test.createOrReplaceTempView("df_test")

        query = "SELECT DISTINCT {col_user} FROM df_test CLUSTER BY {col_user}".format(col_user = self.col_user)
        df_test_users = self.spark.sql(query)
        df_test_users.write.mode("overwrite").saveAsTable("df_test_users")
        df_test_users.cache()
        
        query = "SELECT df_train.* FROM df_train INNER JOIN df_test_users ON df_train.{col_user} = df_test_users.{col_user} CLUSTER BY {col_user}".format(col_user = model.col_user)
        df_train_filtered_test = spark.sql(query)
        df_train_filtered_test.write.mode("overwrite").saveAsTable("df_train_filtered_test")
        df_train_filtered_test.cache()
        
        # user_affinity * item_similarity
        # use CASE to expand upper-triangular to full-matrix
        query = """
        SELECT userID, itemID, score
        FROM
        (
          SELECT userID, itemID, score, row_number() OVER(PARTITION BY userID ORDER BY score DESC) rank
          FROM
          (
            SELECT userID, itemID, score
            FROM
            (
              SELECT df.{col_user} userID,
                     S.i2 itemID,
                     SUM(df.{col_rating} * S.value) AS score,
                     row_number() OVER(PARTITION BY {col_user} ORDER BY SUM(df.{col_rating} * S.value) DESC) rank
              FROM   
                df_train_filtered_test df, 
                item_similarity S
              WHERE df.{col_item} = S.i1
              GROUP BY df.{col_user}, S.i2
            )
            WHERE rank <= {top_k} 

            UNION ALL

            SELECT userID, itemID, score
            FROM
            (
              SELECT df.{col_user} userID,
                     S.i1 itemID,
                     SUM(df.{col_rating} * S.value) AS score,
                     row_number() OVER(PARTITION BY {col_user} ORDER BY SUM(df.{col_rating} * S.value) DESC) rank
              FROM   
                df_train_filtered_test df, 
                item_similarity S
              WHERE df.{col_item} = S.i2 AND S.i1 <> S.i2
              GROUP BY df.{col_user}, S.i1
            )
            WHERE rank <= {top_k} 
          )
        )
        WHERE rank <= {top_k} 
        """.format(col_user = model.col_user, col_item = model.col_item, col_rating = model.col_rating, top_k = 10)

        return self.spark.sql(query)