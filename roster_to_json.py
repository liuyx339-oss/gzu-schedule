import pandas as pd
import json
import math
import os

def generate_roster_json():
    print(">>> 🔄 正在将智能排班表按【科室】拆分并转换为大屏专用 JSON...")
    
    try:
        fixed_df = pd.read_csv("Fixed_Shift_Matrix_With_Names.csv", encoding="utf-8-sig")
        dynamic_df = pd.read_csv("Dynamic_Roster.csv", encoding="utf-8-sig")
    except Exception as e:
        print(f"❌ 读取 CSV 失败: {e}")
        return

    # --- 1. 定义数据分发器 (智能过滤器) ---
    def is_rad(row):
        attr = str(row['人员属性'])
        name = str(row['姓名'])
        # 如果是放射的人员、或是两栖替补，分配给放射科
        if '放射' in attr or '两栖' in attr: return True
        # 如果是缺口警报，必须带有“放射”字眼才显示在放射科大屏
        if '警报' in attr or '缺口' in attr:
            if '放射' in name: return True
            if 'B超' not in name and '超声' not in name: return True 
        return False

    def is_us(row):
        attr = str(row['人员属性'])
        name = str(row['姓名'])
        # 如果是B超或超声的人员、或是两栖替补，分配给B超科
        if 'B超' in attr or '超声' in attr or '两栖' in attr: return True
        # 如果是缺口警报，必须带有“B超”或“超声”字眼才显示在B超科大屏
        if '警报' in attr or '缺口' in attr:
            if 'B超' in name or '超声' in name: return True
            if '放射' not in name: return True 
        return False

    print(">>> ✂️ 正在切割 放射科 与 B超科 的专属数据...")
    # 过滤出放射科专用的矩阵和流水
    rad_fixed = fixed_df[fixed_df.apply(is_rad, axis=1)]
    rad_dynamic = dynamic_df[dynamic_df['科室'].str.contains('放射', na=False)]

    # 过滤出B超专用的矩阵和流水
    us_fixed = fixed_df[fixed_df.apply(is_us, axis=1)]
    us_dynamic = dynamic_df[dynamic_df['科室'].str.contains('B超|超声', na=False)]

    # --- 2. 提取公共的日期与周次 ---
    date_cols = [c for c in fixed_df.columns if "月" in c and "日" in c]
    weeks = {}
    for i, date in enumerate(date_cols):
        week_num = math.floor(i / 7) + 1
        week_key = f"第{week_num}周"
        if week_key not in weeks:
            weeks[week_key] = []
        weeks[week_key].append(date)

    # --- 3. 封装输出函数 ---
    def save_json(f_df, d_df, output_file):
        output_data = {
            "fixed": {
                "columns": date_cols,
                "data": f_df.fillna("休").to_dict(orient="records")
            },
            "dynamic": d_df.fillna("").to_dict(orient="records"),
            "weeks": weeks,
            "week_options": list(weeks.keys())
        }
        with open(output_file, "w", encoding="utf-8") as file:
            json.dump(output_data, file, ensure_ascii=False, indent=4)
        print(f"✅ 成功生成【{output_file}】")

    # 分别保存为两个独立的 JSON 文件
    save_json(rad_fixed, rad_dynamic, "smart_roster_rad.json")
    save_json(us_fixed, us_dynamic, "smart_roster_us.json")
    print("🎉 拆分完毕！您可以前往前端网页修改接口了！")

if __name__ == "__main__":
    generate_roster_json()