# -*- coding: utf-8 -*-
"""生成三份带"真实脏数据"的样例集，用来验证工具好不好使。

数据里故意埋了：缺失、重复、前后空格、异常极值、日期字符串、枚举不一致、
常量列 —— 干净数据看不出工具的差别。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import SAMPLE_DIR

_rng: np.random.Generator | None = None


def rng(seed: int = 7) -> np.random.Generator:
    global _rng
    if _rng is None:
        _rng = np.random.default_rng(seed)
    return _rng


def sample_dates(idx: pd.DatetimeIndex, n: int, gen: np.random.Generator) -> pd.DatetimeIndex:
    """从一串日期里有放回抽样。

    坑一：直接 gen.choice(DatetimeIndex) 拿到的是 numpy.datetime64，
    没有 .strftime / .date 这些方法。

    坑二（更隐蔽）：不要走 idx.asi8 再 to_datetime。pandas 3.0 里
    DatetimeIndex 的默认精度已经变成**微秒**（datetime64[us]），
    asi8 给出来的是微秒整数，而 to_datetime 默认按纳秒解释，
    结果所有日期都会变成 1970 年（差 1000 倍），而且不报错。

    按位置抽索引最稳：天然保留原 dtype 和精度。
    """
    return idx[gen.integers(0, len(idx), size=n)]


CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安"]
CHANNELS = ["自营APP", "小程序", "天猫", "京东", "抖音", "线下门店"]
CATEGORIES = ["手机数码", "家用电器", "服饰鞋包", "美妆护肤", "食品生鲜", "母婴玩具", "图书文娱"]
STATUSES = ["已完成", "已发货", "待付款", "已取消", "退款中"]


def make_orders(n: int = 3000, seed: int = 7) -> pd.DataFrame:
    r = rng(seed)
    dates = pd.date_range("2025-01-01", "2025-12-31", freq="h")
    order_date = sample_dates(dates, n, r)

    user_id = np.arange(100001, 100001 + n)
    city = r.choice(CITIES, size=n, p=[.22, .19, .13, .12, .10, .09, .08, .07])
    channel = r.choice(CHANNELS, size=n, p=[.28, .22, .16, .14, .13, .07])
    category = r.choice(CATEGORIES, size=n, p=[.18, .15, .21, .17, .16, .08, .05])
    status = r.choice(STATUSES, size=n, p=[.62, .14, .10, .09, .05])

    qty = r.integers(1, 6, size=n)
    base = {"手机数码": 3200, "家用电器": 2100, "服饰鞋包": 420, "美妆护肤": 380,
            "食品生鲜": 90, "母婴玩具": 260, "图书文娱": 65}
    price = np.array([base[c] for c in category], dtype=float)
    price *= r.lognormal(0, .28, size=n)
    price = np.round(price, 2)
    amount = np.round(price * qty * (1 - r.choice([0, .05, .1, .15, .2], size=n)), 2)
    cost = np.round(amount * r.uniform(.45, .82, size=n), 2)
    profit = np.round(amount - cost, 2)

    age = r.integers(18, 66, size=n).astype(float)
    member_level = r.choice(["普通", "银卡", "金卡", "钻石"], size=n, p=[.55, .24, .15, .06])
    is_new = r.random(n) < .32
    pay_minutes = np.round(r.exponential(42, size=n) + 1, 1)
    rating = r.choice([1, 2, 3, 4, 5], size=n, p=[.04, .06, .15, .38, .37]).astype(float)
    remark = r.choice(["", "很满意", "物流慢 ", "包装一般", "好评", "  ", "客服态度好"], size=n)

    df = pd.DataFrame({
        "order_id": [f"ORD{202500000+i:09d}" for i in range(n)],
        "user_id": user_id,
        "order_time": order_date,
        "city": city,
        "channel": channel,
        "category": category,
        "status": status,
        "quantity": qty,
        "unit_price": price,
        "amount": amount,
        "cost": cost,
        "profit": profit,
        "member_level": member_level,
        "user_age": age,
        "is_new_user": is_new,
        "pay_duration_min": pay_minutes,
        "rating": rating,
        "remark": remark,
    })

    # ---- 埋脏数据 ----
    # 1) 缺失：城市、评分、备注
    df.loc[r.random(n) < .07, "city"] = np.nan
    df.loc[r.random(n) < .22, "rating"] = np.nan
    df.loc[r.random(n) < .55, "remark"] = np.nan
    # 手机上不填年龄的比例高一些（非随机缺失，值得查）
    df.loc[(df["channel"].isin(["自营APP", "小程序"])) & (r.random(n) < .18), "user_age"] = np.nan

    # 2) 前后空格：让 '北京 ' 和 '北京' 分成两类
    idx = r.choice(n, size=int(n * .05), replace=False)
    df.loc[idx, "city"] = df.loc[idx, "city"].astype(str) + " "
    idx = r.choice(n, size=int(n * .03), replace=False)
    df.loc[idx, "channel"] = " " + df.loc[idx, "channel"].astype(str)

    # 3) 渠道命名不统一
    df["channel"] = df["channel"].replace({"线下门店": "门店", "抖音": "抖音直播"})

    # 4) 异常极值：金额、单价被输错放大 100 倍
    idx = r.choice(n, size=12, replace=False)
    df.loc[idx, "amount"] = df.loc[idx, "amount"] * 100
    idx = r.choice(n, size=8, replace=False)
    df.loc[idx, "user_age"] = r.choice([0, 1, 199, 250], size=8)

    # 5) 重复行：整行复制
    dup_idx = r.choice(n, size=45, replace=False)
    df = pd.concat([df, df.iloc[dup_idx]], ignore_index=True)

    # 6) 常量列 —— 建模前最该删的那种
    df["source_system"] = "crm_v2"

    # 7) 日期有一部分落成字符串，是最常见的脏数据类型之一
    #    注意必须在 concat 之后再做，否则长度对不上
    df["ship_date"] = pd.to_datetime(df["order_time"]).dt.strftime("%Y/%m/%d")

    return df.sample(frac=1, random_state=seed).reset_index(drop=True)


def make_users(n: int = 1200, seed: int = 13) -> pd.DataFrame:
    r = rng(seed)
    reg = pd.date_range("2023-01-01", "2025-12-31", freq="D")
    df = pd.DataFrame({
        "user_id": np.arange(100001, 100001 + n),
        "nickname": [f"用户_{i:05d}" for i in range(n)],
        "gender": r.choice(["男", "女", "未知"], size=n, p=[.48, .46, .06]),
        "city": r.choice(CITIES, size=n),
        "register_date": sample_dates(reg, n, r),
        "age": r.integers(16, 70, size=n).astype(float),
        "total_orders": r.integers(0, 40, size=n),
        "total_amount": np.round(r.gamma(2.2, 900, size=n), 2),
        "last_login_days": r.integers(0, 400, size=n),
        "is_active": r.random(n) < .64,
    })
    df.loc[r.random(n) < .12, "age"] = np.nan
    df.loc[r.random(n) < .09, "city"] = np.nan
    df.loc[r.random(n) < .05, "total_amount"] = 0
    # 注册日期有 6% 是空的
    df.loc[r.random(n) < .06, "register_date"] = pd.NaT
    # 同一手机号被注册多次
    df["phone"] = [f"138{r.integers(10000000, 99999999):08d}" for _ in range(n)]
    df.loc[r.choice(n, size=30, replace=False), "phone"] = "13800000000"
    return df


def make_ads(n: int = 1800, seed: int = 21) -> pd.DataFrame:
    r = rng(seed)
    dates = pd.date_range("2025-06-01", "2025-11-30", freq="D")
    return pd.DataFrame({
        "date": sample_dates(dates, n, r),
        "channel": r.choice(["搜索广告", "信息流", "开屏", "KOL", "私域"], size=n),
        "impression": r.integers(1000, 900_000, size=n),
        "click": r.integers(10, 42_000, size=n),
        "cost": np.round(r.uniform(80, 60_000, size=n), 2),
        "conversion": r.integers(0, 1_800, size=n),
        "revenue": np.round(r.uniform(0, 320_000, size=n), 2),
    }).sort_values("date").reset_index(drop=True)


SAMPLES = {
    "ecommerce_orders": ("电商订单明细（含脏数据）", make_orders),
    "user_profile": ("用户画像（含缺失/重复手机号）", make_users),
    "ad_daily": ("广告投放日报", make_ads),
}


def ensure_samples(force: bool = False) -> dict:
    """把样例落成 csv，返回一个 {key: {path, rows, cols, label}}。"""
    out = {}
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    for key, (label, fn) in SAMPLES.items():
        path = SAMPLE_DIR / f"{key}.csv"
        if force or not path.exists():
            df = fn()
            df.to_csv(path, index=False, encoding="utf-8-sig")
        else:
            df = pd.read_csv(path, nrows=1)
        out[key] = {"key": key, "label": label, "path": str(path)}
    return out


def load_sample(key: str) -> pd.DataFrame:
    path = SAMPLE_DIR / f"{key}.csv"
    if not path.exists():
        ensure_samples()
    return pd.read_csv(path, encoding="utf-8-sig")
