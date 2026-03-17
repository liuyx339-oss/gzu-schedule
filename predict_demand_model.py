import pandas as pd
import numpy as np
from datetime import datetime

def get_period(day):
    if day <= 10: return '上旬'
    elif day <= 20: return '中旬'
    else: return '下旬'

def calculate_probability_matrix(heatmap_file):
    try:
        df_heat = pd.read_csv(heatmap_file, index_col='Hour')
        df_heat.columns = df_heat.columns.astype(int)
        prob_matrix = pd.DataFrame(index=df_heat.index, columns=df_heat.columns)
        for day in df_heat.columns:
            daily_total = df_heat[day].sum()
            prob_matrix[day] = df_heat[day] / daily_total if daily_total > 0 else 0.0
        return prob_matrix
    except Exception as e:
        print(f"⚠️ 无法读取热力图: {e}")
        return None

def build_bottom_up_prediction():
    print(">>> 🚀 启动 [V4 终极引擎]：基于科室/具体小项目颗粒度的推演...")
    
    try:
        df = pd.read_csv("Cleaned_Demand_Details.csv")
    except FileNotFoundError:
        print("❌ 找不到原始清洗数据！")
        return

    df['Date'] = pd.to_datetime(df['Date'])
    df['Period'] = df['Date'].dt.day.apply(get_period)

    # 假设我们预测的目标是“中旬”（真实场景可根据下周的实际日期动态计算）
    target_period = '中旬'
    print(f">>> 📅 设定下周预测周期特征为: 【{target_period}】")

    # 1. 计算历史周期里，每一天出现了多少次 (用于求日均)
    date_counts = df.groupby(['Period', 'DayOfWeek'])['Date'].nunique().reset_index()
    date_counts.rename(columns={'Date': 'DaysCount'}, inplace=True)

    # 2. 【核心落地】：统计每个大类、科室、具体项目在不同星期的总单量
    print(">>> 🔬 正在深挖科室开单明细，计算小项目精准净耗时...")
    item_stats = df.groupby(['大分类', 'eps_dept_desc', 'order_item_desc', 'Period', 'DayOfWeek']).agg({
        '预估医生写报告时长': 'first', 
        '预估操作时长': 'first',
        'Date': 'count' # 这里的 count 就是历史总单量
    }).reset_index()
    item_stats.rename(columns={'Date': 'Total_Volume'}, inplace=True)

    # 3. 算出“日均具体项目单量”
    item_stats = pd.merge(item_stats, date_counts, on=['Period', 'DayOfWeek'], how='left')
    item_stats['Avg_Daily_Volume'] = item_stats['Total_Volume'] / item_stats['DaysCount']

    # 4. 【乘法运算】：日均具体项目数量 × 对应小项目的精确耗时
    item_stats['Pred_Doc_Mins'] = item_stats['Avg_Daily_Volume'] * item_stats['预估医生写报告时长']
    item_stats['Pred_Tech_Mins'] = item_stats['Avg_Daily_Volume'] * item_stats['预估操作时长']

    # 提取目标周期的数据并汇总成按天的大盘总耗时
    target_stats = item_stats[item_stats['Period'] == target_period]
    daily_dept_pred = target_stats.groupby(['大分类', 'DayOfWeek'])[['Pred_Doc_Mins', 'Pred_Tech_Mins']].sum().reset_index()

    # 5. 加载热力图，进行小时级潮汐映射
    rad_prob = calculate_probability_matrix("Heatmap_Radiology_Hourly.csv")
    us_prob = calculate_probability_matrix("Heatmap_Ultrasound_Hourly.csv")

    days_map = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
    predictions = []

    print(">>> 🌊 正在将耗时匹配潮汐曲线，生成24小时兵力部署图...")
    for category, dept_name, prob_matrix in [('放射', 'Radiology', rad_prob), ('超声', 'Ultrasound', us_prob)]:
        cat_data = daily_dept_pred[daily_dept_pred['大分类'] == category]
        
        for _, row in cat_data.iterrows():
            day_idx = int(row['DayOfWeek'])
            d_mins, t_mins = row['Pred_Doc_Mins'], row['Pred_Tech_Mins']
            
            for hour in range(24):
                prob = prob_matrix.loc[hour, day_idx] if (prob_matrix is not None and hour in prob_matrix.index) else 0
                h_d, h_t = d_mins * prob, t_mins * prob
                
                if h_d > 0 or h_t > 0:
                    predictions.append({
                        "Department": dept_name, "DayOfWeek": day_idx, "DayName": days_map[day_idx],
                        "Hour": hour, "Pred_Doc_Mins": round(h_d, 1), "Pred_Tech_Mins": round(h_t, 1)
                    })

    pred_df = pd.DataFrame(predictions).sort_values(['Department', 'DayOfWeek', 'Hour'])
    pred_df.to_csv("Next_Week_Hourly_Prediction.csv", index=False, encoding='utf-8-sig')
    print("✅ 第一步完成：基于【小项目特征】的自下而上预测数据已保存至 Next_Week_Hourly_Prediction.csv")

if __name__ == '__main__':
    build_bottom_up_prediction()