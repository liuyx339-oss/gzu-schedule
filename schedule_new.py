import pandas as pd
import numpy as np
import math
import warnings
import os

warnings.filterwarnings("ignore")

# ==========================================
# 1. 医院资源库与人员画像设定
# ==========================================
STAFF = {
    "rad_docs_full": ["Dustin Huang", "Li Zhenhuan"],
    "rad_docs_pt": [
        "Zhou ChunXiang",
        "Liang ZhiYing",
        "Ling Jian",
        "Liang Ruiyun",
        "Liu Zengwei",
        "Chen Yingxi",
    ],
    "rad_techs_full": [
        "Zheng Xiaochun",
        "Zhang Meng",
        "Ma Linlin",
        "Yang Yongjun",
        "Yi Hong",
        "Liu Shuting",
    ],
    "rad_techs_pt": ["ZHONG Minzhi", "LUO Hui", "CHEN Jiajun"],
    "us_docs_full": ["Xu Jing", "Liu Xiaoyan", "Lu Liyu", "Dustin Huang"],
    "us_docs_expert": ["Li anhua"],
}

ROLE_MAP = {
    "rad_docs_full": "放射医生(全职)",
    "rad_docs_pt": "放射医生(兼职)",
    "rad_techs_full": "放射技师(全职)",
    "rad_techs_pt": "放射技师(兼职)",
    "us_docs_full": "B超医生(全职)",
    "us_docs_expert": "B超专家",
}

SORT_WEIGHT = {
    "放射医生(全职)": 1,
    "放射医生(兼职)": 2,
    "放射技师(全职)": 3,
    "放射技师(兼职)": 4,
    "B超医生(全职)": 5,
    "B超专家": 6,
    "两栖替补(放射+B超)": 0,
    "⚠️系统警报(缺口)": 99,
}

TARGET_HOURS = {}
for p in STAFF["rad_docs_full"] + STAFF["rad_techs_full"] + STAFF["us_docs_full"]:
    TARGET_HOURS[p] = 176.0
for p in STAFF["rad_docs_pt"]:
    TARGET_HOURS[p] = 60.0
for p in STAFF["rad_techs_pt"]:
    TARGET_HOURS[p] = 80.0
TARGET_HOURS["Li anhua"] = 32.0

# ==========================================
# 2. 原生标准班表字典
# ==========================================
SHIFT_DICT = {
    "D1": (8.5, 17.0, 8.0, "白班"),
    "D": (8.5, 17.5, 8.5, "白班"),
    "D2": (9.0, 17.5, 8.0, "白班"),
    "D3": (9.5, 18.0, 8.0, "白班"),
    "D4": (9.0, 18.0, 8.5, "白班"),
    "D5": (8.5, 18.0, 9.0, "白班"),
    "C": (7.67, 16.17, 8.0, "白班"),
    "C1": (8.0, 16.5, 8.0, "白班"),
    "H1": (7.67, 11.67, 4.0, "半天班"),
    "H2": (8.5, 12.5, 4.0, "半天班"),
    "H3": (13.5, 17.5, 4.0, "半天班"),
    "T": (8.0, 12.0, 4.0, "半天班"),
    "N": (17.5, 32.0, 14.5, "夜班"),
    "L/N": (8.0, 32.0, 24.0, "24H班"),
    "兼职夜": (18.0, 32.0, 14.0, "夜班"),
    "N(OnCall)": (17.5, 32.0, 14.5, "夜班"),
}

# 🌟 新增：班次时间可视化映射字典（用于最终展示）
SHIFT_TIME_STR = {
    "D1": "08:30-17:00",
    "D": "08:30-17:30",
    "D2": "09:00-17:30",
    "D3": "09:30-18:00",
    "D4": "09:00-18:00",
    "D5": "08:30-18:00",
    "C": "07:40-16:10",
    "C1": "08:00-16:30",
    "H1": "07:40-11:40",
    "H2": "08:30-12:30",
    "H3": "13:30-17:30",
    "T": "08:00-12:00",
    "N": "17:30-08:00",
    "L/N": "08:00-08:00",
    "兼职夜": "18:00-08:00",
    "N(OnCall)": "17:30-08:00",
}

DAY_SHIFTS_POOL = ["D5", "D", "D4", "D1", "D2", "D3", "C1", "C"]
HALF_SHIFTS_POOL = ["H1", "H2", "T", "H3"]

# ==========================================
# 3. 负荷率数据（来自3/15需求文档）
# ==========================================
LOAD_RATE = {
    "放射医生": {
        0: 0.930988,
        1: 0.937606,
        2: 0.933171,
        3: 1.0,
        4: 1.0,
        5: 0.955709,
        6: 0.868295,
        7: 0.868396,
        8: 0.776812,
        9: 0.728898,
        10: 0.708848,
        11: 0.711359,
        12: 0.70014,
        13: 0.741611,
        14: 0.756309,
        15: 0.766785,
        16: 0.791551,
        17: 0.808238,
        18: 0.85161,
        19: 0.870316,
        20: 0.857747,
        21: 0.891201,
        22: 0.891172,
        23: 0.926642,
    },
    "放射技师": {
        0: 0.894781,
        1: 0.916714,
        2: 0.93822,
        3: 0.903828,
        4: 0.856494,
        5: 0.930688,
        6: 0.849995,
        7: 0.92332,
        8: 0.86096,
        9: 0.801839,
        10: 0.608063,
        11: 0.743238,
        12: 0.721842,
        13: 0.728564,
        14: 0.727277,
        15: 0.738215,
        16: 0.738477,
        17: 0.794821,
        18: 0.87881,
        19: 0.874276,
        20: 0.858698,
        21: 0.87809,
        22: 0.883282,
        23: 0.911768,
    },
    "B超医生": {
        0: 0.939,
        1: 0.88374,
        2: 0.8804,
        3: 0.939,
        4: 0.939,
        5: 0.939,
        6: 0.939,
        7: 0.9924,
        8: 0.9488,
        9: 0.9692,
        10: 0.8692,
        11: 0.8536,
        12: 0.74042,
        13: 0.72396,
        14: 0.61706,
        15: 0.53536,
        16: 0.54538,
        17: 0.6105,
        18: 0.75438,
        19: 0.86406,
        20: 0.84002,
        21: 0.87574,
        22: 0.87734,
        23: 0.8828,
    },
}


def get_hourly_headcount(workload_mins, dept, role, is_daytime, hour=None):
    """根据负荷率计算每小时需要的人数"""
    if hour is not None:
        role_key = (
            role if role in LOAD_RATE else ("放射医生" if "放射" in role else "B超医生")
        )
        load_rate = LOAD_RATE.get(role_key, {}).get(hour, 0.8)
        effective_mins = 60 * load_rate
        if effective_mins > 0:
            hc = math.ceil(workload_mins / effective_mins)
        else:
            hc = 1
    else:
        hc = math.ceil(workload_mins / 48)

    max_machines = 8 if dept == "放射" else 7
    hc = min(hc, max_machines)
    if is_daytime:
        return max(hc, 2) if role == "放射技师" else max(hc, 1)
    return 1


def run_smart_roster():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_file = os.path.join(base_dir, "Prophet_TopDown_Item_Forecast.csv")
    print(
        f"\n{'=' * 65}\n🚀 启动【靶向工时比竞争模型】排班系统 (带可视化时间段显示)\n{'=' * 65}"
    )
    try:
        df = pd.read_csv(data_file, encoding="utf-8-sig")
    except Exception as e:
        return print(f"❌ 读取数据失败: {e}")

    hourly_df = (
        df.groupby(["预测时间", "大分类"])[
            ["预测_全科室技师总耗时", "预测_全科室医生总耗时"]
        ]
        .first()
        .reset_index()
    )
    hourly_df["预测时间"] = pd.to_datetime(hourly_df["预测时间"])
    hourly_df["Hour"], hourly_df["Date_Str"] = (
        hourly_df["预测时间"].dt.hour,
        hourly_df["预测时间"].dt.strftime("%m月%d日"),
    )
    hourly_df["Is_Daytime"] = hourly_df["Hour"].apply(lambda x: 8 <= x < 18)

    demand_matrix = {}
    for _, row in hourly_df.iterrows():
        d, h, dept = row["Date_Str"], row["Hour"], row["大分类"]
        if d not in demand_matrix:
            demand_matrix[d] = {}
        if dept == "放射":
            for role, time_col in [
                ("放射技师", "预测_全科室技师总耗时"),
                ("放射医生", "预测_全科室医生总耗时"),
            ]:
                key = f"{dept}_{role}"
                if key not in demand_matrix[d]:
                    demand_matrix[d][key] = np.zeros(24)
                demand_matrix[d][key][h] = get_hourly_headcount(
                    row[time_col], dept, role, row["Is_Daytime"], hour=h
                )
        elif dept == "超声":
            key = f"{dept}_B超医生"
            if key not in demand_matrix[d]:
                demand_matrix[d][key] = np.zeros(24)
            demand_matrix[d][key][h] = get_hourly_headcount(
                row["预测_全科室医生总耗时"], dept, "B超医生", row["Is_Daytime"], hour=h
            )

    unique_dates = sorted(list(demand_matrix.keys()))
    all_staff_names = list(set([name for pool in STAFF.values() for name in pool]))
    schedule = {name: {} for name in all_staff_names}
    hours_bank = {name: 0.0 for name in all_staff_names}
    night_counts = {name: 0 for name in all_staff_names}

    # 👨‍⚕️ 预排班：李主任出诊
    for d in unique_dates:
        try:
            dow = pd.to_datetime(d, format="%m月%d日").replace(year=2026).dayofweek + 1
            if dow in [1, 4]:
                schedule["Li anhua"][d] = "H3"
                hours_bank["Li anhua"] += 4.0
                if d in demand_matrix and "超声_B超医生" in demand_matrix[d]:
                    for h in range(13, 18):
                        demand_matrix[d]["超声_B超医生"][h] = max(
                            0, demand_matrix[d]["超声_B超医生"][h] - 1
                        )
        except Exception:
            pass

    def get_valid_candidates(pool_key, date, shift_code):
        cands = []
        for p in STAFF[pool_key]:
            if date in schedule[p]:
                continue

            y_idx = unique_dates.index(date) - 1
            if y_idx >= 0 and SHIFT_DICT[shift_code][3] in ["白班", "半天班"]:
                if schedule[p].get(unique_dates[y_idx], "") in ["N", "L/N", "兼职夜"]:
                    continue

            if (
                SHIFT_DICT[shift_code][3] == "夜班"
                and p in ["Dustin Huang", "Li Zhenhuan"]
                and night_counts[p] >= 2
            ):
                continue
            cands.append(p)
        return cands

    def assign_night_shift(tiered_pools, date, dept):
        for pool_key, shift_code in tiered_pools:
            cands = get_valid_candidates(pool_key, date, shift_code)
            if cands:
                chosen = min(
                    cands, key=lambda x: hours_bank[x] / TARGET_HOURS.get(x, 176.0)
                )
                schedule[chosen][date] = shift_code
                hours_bank[chosen] += SHIFT_DICT[shift_code][2]
                night_counts[chosen] += 1
                return True
        v = f"🚨缺口(夜班缺{dept})_{len(schedule) + 1}"
        if v not in all_staff_names:
            all_staff_names.append(v)
            schedule[v] = {}
            hours_bank[v] = 0
            night_counts[v] = 0
        schedule[v][date] = tiered_pools[-1][1]
        return False

    def assign_day_shift(combined_pools, date, dept, shift_code):
        cands = []
        for pool_key in combined_pools:
            cands.extend(get_valid_candidates(pool_key, date, shift_code))

        if cands:
            chosen = min(
                cands, key=lambda x: hours_bank[x] / TARGET_HOURS.get(x, 176.0)
            )
            schedule[chosen][date] = shift_code
            hours_bank[chosen] += SHIFT_DICT[shift_code][2]
            return True

        v = f"🚨缺口(白班缺{dept})_{len(schedule) + 1}"
        if v not in all_staff_names:
            all_staff_names.append(v)
            schedule[v] = {}
            hours_bank[v] = 0
            night_counts[v] = 0
        schedule[v][date] = shift_code
        return False

    print(">>> ⚖️ 正在执行靶向比率拼图，彻底释放所有产能...")
    for date in unique_dates:
        if "放射_放射医生" in demand_matrix[date]:
            assign_night_shift(
                [("rad_docs_pt", "兼职夜"), ("rad_docs_full", "N")], date, "放射"
            )
            for h in list(range(18, 24)) + list(range(0, 8)):
                demand_matrix[date]["放射_放射医生"][h] = max(
                    0, demand_matrix[date]["放射_放射医生"][h] - 1
                )
        if "放射_放射技师" in demand_matrix[date]:
            assign_night_shift([("rad_techs_full", "N")], date, "放射")
            for h in list(range(18, 24)) + list(range(0, 8)):
                demand_matrix[date]["放射_放射技师"][h] = max(
                    0, demand_matrix[date]["放射_放射技师"][h] - 1
                )
        if "超声_B超医生" in demand_matrix[date]:
            assign_night_shift([("us_docs_full", "N(OnCall)")], date, "超声")
            for h in list(range(18, 24)) + list(range(0, 8)):
                demand_matrix[date]["超声_B超医生"][h] = max(
                    0, demand_matrix[date]["超声_B超医生"][h] - 1
                )

        def cover_day(key, dept, combined_pools):
            if key not in demand_matrix[date]:
                return
            arr = demand_matrix[date][key]
            while np.sum(arr[8:18]) > 0:
                best_shift, max_cov = None, -1
                for s in DAY_SHIFTS_POOL:
                    cov = sum(
                        [
                            1
                            for h in range(8, 18)
                            if SHIFT_DICT[s][0] <= h < SHIFT_DICT[s][1] and arr[h] > 0
                        ]
                    )
                    if cov > max_cov:
                        max_cov = cov
                        best_shift = s
                if max_cov < 4:
                    for s in HALF_SHIFTS_POOL:
                        cov = sum(
                            [
                                1
                                for h in range(8, 18)
                                if SHIFT_DICT[s][0] <= h < SHIFT_DICT[s][1]
                                and arr[h] > 0
                            ]
                        )
                        if cov > max_cov:
                            max_cov = cov
                            best_shift = s
                if max_cov <= 0:
                    break

                assign_day_shift(combined_pools, date, dept, best_shift)

                st, ed = SHIFT_DICT[best_shift][0], SHIFT_DICT[best_shift][1]
                for h in range(8, 18):
                    if st <= h < ed:
                        arr[h] = max(0, arr[h] - 1)

        cover_day("超声_B超医生", "B超医生", ["us_docs_full"])
        cover_day("放射_放射技师", "放射技师", ["rad_techs_full", "rad_techs_pt"])
        cover_day("放射_放射医生", "放射医生", ["rad_docs_full", "rad_docs_pt"])

    # --- 输出并拼装带时间段的可视化字符串 ---
    res = []
    for person in all_staff_names:
        row = {"姓名": person}
        if "🚨" in person:
            attr_name = "⚠️系统警报(缺口)"
        else:
            groups = [k for k, v in STAFF.items() if person in v]
            attr_name = (
                "两栖替补(放射+B超)"
                if len(groups) > 1
                else ROLE_MAP.get(groups[0], groups[0])
            )

        row["人员属性"] = attr_name
        row["本期总工时"] = hours_bank.get(person, 0.0)
        row["Sort_Key"] = SORT_WEIGHT.get(attr_name, 50)

        for d in unique_dates:
            raw_shift = schedule[person].get(d, "休")
            # 💡 核心视觉翻译器：如果是有效班次，在代号后加上括号时间
            if raw_shift in SHIFT_TIME_STR:
                row[d] = f"{raw_shift}({SHIFT_TIME_STR[raw_shift]})"
            else:
                row[d] = raw_shift

        res.append(row)

    df_out = (
        pd.DataFrame(res)
        .sort_values(by=["Sort_Key", "姓名"])
        .drop(columns=["Sort_Key"])
    )
    output_file = os.path.join(base_dir, "Fixed_Shift_Matrix_With_Names.csv")
    df_out.to_csv(output_file, index=False, encoding="utf-8-sig")
    print("✅ 包含【时间段精准展示】的完美排班矩阵已生成！")


if __name__ == "__main__":
    run_smart_roster()
