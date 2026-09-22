"""样例电商库造数脚本。

设计要点：
1. 固定随机种子，同一份代码在任何机器上生成的库完全一致，评测结果才可复现；
2. 数据带业务规律而非纯随机：订单量按月递增并叠加周内波动与促销峰值，
   各区域、各渠道、各品类的占比不同，这样生成的图表才有可解读的形态；
3. 时间范围覆盖 2025-01-01 至造数当日，保证「上月」「上季度」「近 30 天」这类
   相对时间问法都能命中数据。

用法：
    python data/seed.py             # 库已存在则覆盖重建
    python data/seed.py --keep      # 库已存在则跳过
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "ecommerce.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"

SEED = 20260914

# 数据规模。演示级，控制在万行量级，SQLite 完全够用。
N_USERS = 2000
N_PRODUCTS = 500
N_ORDERS = 12000

START_DATE = dt.date(2025, 1, 1)

REGIONS = ["华东", "华南", "华北", "华中", "西南", "西北", "东北"]
# 各区域权重，华东华南是主力市场
REGION_WEIGHTS = [28, 22, 18, 12, 9, 6, 5]

CHANNELS = ["app", "h5", "miniprogram", "pc"]
CHANNEL_WEIGHTS = [45, 18, 27, 10]
CHANNEL_LABELS = {"app": "App", "h5": "H5", "miniprogram": "小程序", "pc": "PC"}

LEVELS = ["normal", "silver", "gold", "platinum"]
LEVEL_WEIGHTS = [58, 26, 12, 4]

# 一级类目 -> 二级类目，二级类目的 parent_id 指向一级类目
CATEGORY_TREE: dict[str, list[str]] = {
    "服饰鞋包": ["女装", "男装", "运动鞋", "箱包"],
    "数码家电": ["手机", "笔记本电脑", "智能穿戴", "生活电器"],
    "美妆个护": ["面部护肤", "彩妆", "洗护发", "身体护理"],
    "食品生鲜": ["休闲零食", "粮油调味", "乳品烘焙", "新鲜水果"],
    "家居家装": ["床上用品", "厨具", "收纳整理", "灯具"],
    "母婴玩具": ["婴儿服饰", "喂养用品", "益智玩具", "童鞋"],
}

BRANDS = [
    "启元", "沐光", "fisher", "北岸", "云谷", "拾光", "南屿", "青野",
    "tide", "木白", "长风", "果壳", "灰石", "闻笛", "mori", "初禾",
]

REFUND_REASONS = ["七天无理由", "质量问题", "发错货", "买家拒收"]
REFUND_REASON_WEIGHTS = [58, 24, 10, 8]

# 促销月：3 月（春季大促）、6 月（年中）、11 月、12 月，倍数明显高于平常
PROMO_MONTHS = {3: 1.35, 6: 1.55, 11: 1.9, 12: 1.45}

# 未支付订单约 12%，取消约 5%，退款约 4%，其余按已支付/已发货/已完成分布
STATUS_POOL = ["pending", "paid", "shipped", "completed", "cancelled", "refunded"]
STATUS_WEIGHTS = [12, 18, 20, 41, 5, 4]


def _month_factor(day: dt.date) -> float:
    """月度订单量倍数：整体缓慢增长 + 促销月峰值。"""
    months_since_start = (day.year - START_DATE.year) * 12 + (day.month - START_DATE.month)
    growth = 1.0 + 0.022 * months_since_start
    promo = PROMO_MONTHS.get(day.month, 1.0)
    return growth * promo


def build_users(rng: random.Random) -> list[tuple]:
    names = [
        "王", "李", "张", "刘", "陈", "杨", "黄", "赵", "周", "吴",
        "徐", "孙", "马", "朱", "胡", "郭", "何", "林", "高", "罗",
    ]
    given = [
        "伟", "芳", "娜", "敏", "静", "磊", "洋", "勇", "艳", "杰",
        "涛", "明", "超", "秀英", "霞", "平", "刚", "桂英", "文", "辉",
    ]
    cities = [
        "上海", "北京", "深圳", "广州", "杭州", "成都", "武汉", "南京",
        "西安", "重庆", "苏州", "长沙", "青岛", "合肥", "天津", "郑州",
    ]
    rows = []
    for uid in range(1, N_USERS + 1):
        # 注册日期落在业务起始日之前到当天之间，老用户占比更高
        offset = int(abs(rng.gauss(0, 1)) * 150)
        reg = START_DATE - dt.timedelta(days=offset)
        # phone / id_card 由 uid 确定性派生，绝不取 rng：造数逻辑里固定种子
        # 承载着评测基线，多取一次随机数会扰动后续所有分布。
        phone = "139" + f"{uid:08d}"
        id_card = f"310100{reg:%Y%m%d}{uid:03d}X"
        rows.append(
            (
                uid,
                rng.choice(names) + rng.choice(given),
                rng.choice(cities),
                rng.choices(LEVELS, LEVEL_WEIGHTS)[0],
                rng.choices(CHANNELS, CHANNEL_WEIGHTS)[0],
                reg.isoformat(),
                phone,
                id_card,
            )
        )
    return rows


def build_categories() -> tuple[list[tuple], list[tuple]]:
    """返回 (一级类目, 二级类目) 两组行。id 连续分配，二级类目 parent_id 指向一级。"""
    level1, level2 = [], []
    cid = 1
    for top_name, children in CATEGORY_TREE.items():
        level1.append((cid, top_name, None))
        parent = cid
        cid += 1
        for child in children:
            level2.append((cid, child, parent))
            cid += 1
    return level1, level2


def build_products(rng: random.Random, leaf_categories: list[tuple]) -> list[tuple]:
    rows = []
    adjectives = ["经典款", "轻奢", "入门", "旗舰", "联名", "简约", "复古", "加厚"]
    for pid in range(1, N_PRODUCTS + 1):
        cid, cname, _parent = rng.choice(leaf_categories)
        cost = round(rng.uniform(18, 1600), 2)
        # 标价按类目浮动，毛利倍率 1.8 - 3.2
        list_price = round(cost * rng.uniform(1.8, 3.2), 2)
        rows.append(
            (
                pid,
                f"{rng.choice(BRANDS)} {cname} {rng.choice(adjectives)}{rng.randint(1, 99):02d}",
                cid,
                rng.choice(BRANDS),
                cost,
                list_price,
            )
        )
    return rows


def build_orders_and_items(
    rng: random.Random, products: list[tuple], end_date: dt.date
) -> tuple[list[tuple], list[tuple], list[tuple]]:
    """生成订单、订单明细与退款。

    返回 (orders, order_items, refunds)。三张表在同一循环内生成，
    保证明细与订单、退款与订单的引用关系一致。
    """
    orders: list[tuple] = []
    items: list[tuple] = []
    refunds: list[tuple] = []
    item_id = 1
    refund_id = 1

    # 按天分配订单量：先算每日的期望倍数，再归一化到 N_ORDERS 总量
    total_days = (end_date - START_DATE).days + 1
    day_list = [START_DATE + dt.timedelta(days=i) for i in range(total_days)]
    weights = [_month_factor(d) for d in day_list]
    # 周末订单量高一些
    weights = [w * (1.18 if d.weekday() >= 5 else 1.0) for w, d in zip(weights, day_list)]
    wsum = sum(weights)
    per_day = [w / wsum * N_ORDERS for w in weights]

    # 商品的热度不均：前 20% 商品贡献约一半销量
    product_ids = [p[0] for p in products]
    popularity = [1.0 / (i + 1) ** 0.6 for i in range(len(product_ids))]
    psum = sum(popularity)

    oid = 0
    for day, expected in zip(day_list, per_day):
        n_today = int(expected) + (1 if rng.random() < (expected % 1) else 0)
        for _ in range(n_today):
            oid += 1
            status = rng.choices(STATUS_POOL, STATUS_WEIGHTS)[0]
            # 当天订单不会立刻完成，近 7 天的订单状态收敛到待支付/已支付/已发货
            if (end_date - day).days < 7:
                status = rng.choices(
                    ["pending", "paid", "shipped", "completed", "cancelled"],
                    [22, 30, 28, 15, 5],
                )[0]
            hour = rng.choices(
                range(24),
                [1, 1, 1, 1, 1, 1, 2, 3, 5, 7, 8, 8, 7, 6, 6, 6, 7, 8, 10, 12, 13, 11, 6, 3],
            )[0]
            created = dt.datetime.combine(
                day, dt.time(hour, rng.randint(0, 59), rng.randint(0, 59))
            )
            paid_at = None
            if status != "pending" and status != "cancelled":
                paid_at = (created + dt.timedelta(minutes=rng.randint(1, 90))).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            orders.append(
                (
                    oid,
                    rng.randint(1, N_USERS),
                    status,
                    rng.choices(REGIONS, REGION_WEIGHTS)[0],
                    rng.choices(CHANNELS, CHANNEL_WEIGHTS)[0],
                    created.strftime("%Y-%m-%d %H:%M:%S"),
                    paid_at,
                )
            )

            # 每单 1-5 个商品，多为 1-2 个
            n_items = rng.choices([1, 2, 3, 4, 5], [46, 27, 15, 8, 4])[0]
            chosen = set()
            order_amount = 0.0
            for _i in range(n_items):
                pid = rng.choices(product_ids, weights=popularity)[0]
                if pid in chosen:
                    continue
                chosen.add(pid)
                product = products[pid - 1]
                list_price = product[5]
                # 成交价在标价基础上打折，折扣力度与订单商品数无关
                discount = rng.choices([1.0, 0.95, 0.9, 0.85, 0.8, 0.7], [28, 20, 20, 15, 12, 5])[0]
                unit_price = round(list_price * discount, 2)
                quantity = rng.choices([1, 2, 3, 4, 5, 8], [58, 22, 11, 5, 3, 1])[0]
                items.append((item_id, oid, pid, quantity, unit_price))
                item_id += 1
                order_amount += unit_price * quantity

            # 已退款订单挂一条退款记录；另有少量非退款订单发生部分退款
            if status == "refunded":
                refunds.append(
                    (
                        refund_id,
                        oid,
                        round(order_amount, 2),
                        rng.choices(REFUND_REASONS, REFUND_REASON_WEIGHTS)[0],
                        (created + dt.timedelta(days=rng.randint(1, 12))).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                    )
                )
                refund_id += 1
            elif rng.random() < 0.012:
                refunds.append(
                    (
                        refund_id,
                        oid,
                        round(order_amount * rng.uniform(0.2, 0.6), 2),
                        rng.choices(REFUND_REASONS, REFUND_REASON_WEIGHTS)[0],
                        (created + dt.timedelta(days=rng.randint(1, 12))).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                    )
                )
                refund_id += 1

    return orders, items, refunds


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 Text2SQL 样例电商库")
    parser.add_argument("--keep", action="store_true", help="库已存在时跳过，不覆盖")
    parser.add_argument("--db", default=str(DB_PATH), help="输出库路径")
    parser.add_argument(
        "--end-date",
        help="数据截止日（YYYY-MM-DD），默认昨天；评测基线快照复现时显式指定",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if db_path.exists() and args.keep:
        print(f"[skip] 库已存在：{db_path}")
        return

    rng = random.Random(SEED)
    if args.end_date:
        end_date = dt.date.fromisoformat(args.end_date)
    else:
        end_date = dt.date.today() - dt.timedelta(days=1)

    print(f"[1/5] 建表 {SCHEMA_PATH.name}")
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    print(f"[2/5] 生成用户 {N_USERS} 条")
    users = build_users(rng)
    conn.executemany("INSERT INTO users VALUES (?,?,?,?,?,?,?,?)", users)

    print("[3/5] 生成类目与商品")
    level1, level2 = build_categories()
    conn.executemany("INSERT INTO categories VALUES (?,?,?)", level1 + level2)
    leaf = [(c[0], c[1], c[2]) for c in level2]
    products = build_products(rng, leaf)
    conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", products)

    print(f"[4/5] 生成订单 / 明细 / 退款（{START_DATE} 至 {end_date}）")
    orders, items, refunds = build_orders_and_items(rng, products, end_date)
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", orders)
    conn.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", items)
    conn.executemany("INSERT INTO refunds VALUES (?,?,?,?,?)", refunds)

    print("[5/5] 建索引并统计")
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()

    for table in ("users", "categories", "products", "orders", "order_items", "refunds"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"      {table:<12} {n:>7} 行")
    conn.close()
    size_mb = db_path.stat().st_size / 1024 / 1024
    print(f"[done] {db_path}  （{size_mb:.2f} MB，随机种子 {SEED}）")


if __name__ == "__main__":
    main()
