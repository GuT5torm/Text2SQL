-- Text2SQL 电商样例库建表语句
-- 执行方式：由 data/seed.py 读取后建库，不手工执行
-- 约定：
--   1. 时间字段统一存 ISO8601 字符串 'YYYY-MM-DD HH:MM:SS'，便于 SQLite strftime 直接解析
--   2. 金额单位为元，存 REAL；数量存 INTEGER
--   3. 表名与字段名使用英文，中文显示名在 semantic/tables.yaml 中维护
--   4. 业务库以只读方式打开，本文件仅在造数时使用

DROP TABLE IF EXISTS refunds;
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS categories;
DROP TABLE IF EXISTS users;

-- 用户表
CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,
    city          TEXT    NOT NULL,
    level         TEXT    NOT NULL,   -- 会员等级：normal / silver / gold / platinum
    channel       TEXT    NOT NULL,   -- 注册渠道：app / h5 / miniprogram / pc
    register_date TEXT    NOT NULL,   -- 注册日期 YYYY-MM-DD
    phone         TEXT,               -- 手机号（敏感字段，出参必须脱敏）
    id_card       TEXT                -- 身份证号（敏感字段，出参必须脱敏）
);

-- 商品类目表，parent_id 为空表示一级类目
CREATE TABLE categories (
    id        INTEGER PRIMARY KEY,
    name      TEXT    NOT NULL,
    parent_id INTEGER,
    FOREIGN KEY (parent_id) REFERENCES categories(id)
);

-- 商品表
CREATE TABLE products (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    category_id INTEGER NOT NULL,
    brand       TEXT    NOT NULL,
    cost_price  REAL    NOT NULL,   -- 成本价
    list_price  REAL    NOT NULL,   -- 标价
    FOREIGN KEY (category_id) REFERENCES categories(id)
);

-- 订单表，一行一个订单
CREATE TABLE orders (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    status     TEXT    NOT NULL,    -- pending / paid / shipped / completed / cancelled / refunded
    region     TEXT    NOT NULL,    -- 销售大区：华东 / 华南 / 华北 / 华中 / 西南 / 西北 / 东北
    channel    TEXT    NOT NULL,    -- 下单渠道：app / h5 / miniprogram / pc
    created_at TEXT    NOT NULL,    -- 下单时间
    paid_at    TEXT,                -- 支付时间，未支付为空
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- 订单明细表，一行一个订单中的一个商品
CREATE TABLE order_items (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity   INTEGER NOT NULL,
    unit_price REAL    NOT NULL,    -- 成交单价，已含折扣
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (product_id) REFERENCES products(id)
);

-- 退款表
CREATE TABLE refunds (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL,
    amount     REAL    NOT NULL,
    reason     TEXT    NOT NULL,    -- 七天无理由 / 质量问题 / 发错货 / 买家拒收
    created_at TEXT    NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

-- 索引：覆盖语义层常用的过滤与聚合维度
CREATE INDEX idx_orders_created_at ON orders(created_at);
CREATE INDEX idx_orders_region     ON orders(region);
CREATE INDEX idx_orders_status     ON orders(status);
CREATE INDEX idx_orders_channel    ON orders(channel);
CREATE INDEX idx_orders_user       ON orders(user_id);
CREATE INDEX idx_items_order       ON order_items(order_id);
CREATE INDEX idx_items_product     ON order_items(product_id);
CREATE INDEX idx_products_category ON products(category_id);
CREATE INDEX idx_refunds_order     ON refunds(order_id);
CREATE INDEX idx_refunds_created   ON refunds(created_at);
