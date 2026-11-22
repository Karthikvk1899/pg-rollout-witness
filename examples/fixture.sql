CREATE TABLE orders(id int PRIMARY KEY, reference text NOT NULL);
INSERT INTO orders SELECT i, md5(current_setting('witness.seed') || ':' || i)
FROM generate_series(1,1000) AS i;
