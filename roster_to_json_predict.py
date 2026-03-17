import pandas as pd
import json
import math
import os

def generate_predict_json():
    print(">>> 🔄 正在将【原固定排班】与【预测班次矩阵】打包为全新的 JSON...")
    
    fixed_file = "Fixed_Shift_Matrix_With_Names.csv"
    predict_file = "Shift_Matrix_With_Names_Predict.csv"
    
    try:
        fixed_df = pd.read_csv(fixed_file, encoding="utf-8-sig")
        predict_df = pd.read_csv(predict_file, encoding="utf-8-sig")
    except Exception as e:
        print(f"❌ 读取 CSV 失败，请确保 {fixed_file} 和 {predict_file} 都在文件夹中: {e}")
        return

    # --- 智能过滤器：区分科室 ---
    def is_rad(row):
        attr, name = str(row.get('人员属性', '')), str(row.get('姓名', ''))
        if '放射' in attr or '两栖' in attr: return True
        if '警报' in attr or '缺口' in attr:
            if '放射' in name: return True
            if 'B超' not in name and '超声' not in name: return True 
        return False

    def is_us(row):
        attr, name = str(row.get('人员属性', '')), str(row.get('姓名', ''))
        if 'B超' in attr or '超声' in attr or '两栖' in attr: return True
        if '警报' in attr or '缺口' in attr:
            if 'B超' in name or '超声' in name: return True
            if '放射' not in name: return True 
        return False

    # 切分两份矩阵数据
    rad_fixed = fixed_df[fixed_df.apply(is_rad, axis=1)]
    us_fixed = fixed_df[fixed_df.apply(is_us, axis=1)]
    
    rad_predict = predict_df[predict_df.apply(is_rad, axis=1)]
    us_predict = predict_df[predict_df.apply(is_us, axis=1)]

    # 提取日期与周次
    date_cols = [c for c in fixed_df.columns if "月" in c and "日" in c]
    weeks = {}
    for i, date in enumerate(date_cols):
        week_num = math.floor(i / 7) + 1
        week_key = f"第{week_num}周"
        if week_key not in weeks: weeks[week_key] = []
        weeks[week_key].append(date)

    # 封装输出结构：将原本的 dynamic 替换为 predict
    def save_json(f_df, p_df, output_file):
        output_data = {
            "fixed": {"columns": date_cols, "data": f_df.fillna("休").to_dict(orient="records")},
            "predict": {"columns": date_cols, "data": p_df.fillna("休").to_dict(orient="records")},
            "weeks": weeks,
            "week_options": list(weeks.keys())
        }
        with open(output_file, "w", encoding="utf-8") as file:
            json.dump(output_data, file, ensure_ascii=False, indent=4)
        print(f"✅ 成功打包: {output_file}")

    save_json(rad_fixed, rad_predict, "smart_roster_rad.json")
    save_json(us_fixed, us_predict, "smart_roster_us.json")
    print("🎉 JSON 数据生成完毕！")

if __name__ == "__main__":
    generate_predict_json()