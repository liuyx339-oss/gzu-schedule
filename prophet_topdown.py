import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from prophet import Prophet
import warnings

warnings.filterwarnings("ignore")

def run_prophet_topdown_forecast(input_file="New_Cleaned_Demand_Details.csv", forecast_days=30):
    print(f"\n{'='*60}")
    print(f"🚀 启动【Prophet 总量预测 + 历史画像指纹分配】终极引擎")
    print(f"{'='*60}")
    
    # ==========================================
    # 1. 读取与清洗数据
    # ==========================================
    try:
        df = pd.read_csv(input_file, encoding="utf-8-sig")
    except Exception as e:
        print(f"❌ 读取数据失败: {e}")
        return

    df = df.dropna(subset=['arrived_datetime']).copy()
    df['arrived_datetime'] = pd.to_datetime(df['arrived_datetime'], errors='coerce')
    df = df.dropna(subset=['arrived_datetime']).copy()
    
    # 抹平到小时，并提取“星期”和“小时”作为指纹特征
    df['ds'] = df['arrived_datetime'].dt.floor('H')
    df['DayOfWeek'] = df['ds'].dt.dayofweek
    df['Hour'] = df['ds'].dt.hour
    df['order_item_desc'] = df['order_item_desc'].fillna('Unknown_Item')

    # ==========================================
    # 2. 提取“历史画像指纹” (核心逻辑)
    # ==========================================
    print(">>> 🧬 正在提取全科室时空历史画像指纹 (Fingerprints)...")
    
    # 算微观：计算历史中【每个特定星期几 + 特定小时 + 特定项目】的总耗时
    item_grp = df.groupby(['DayOfWeek', 'Hour', 'order_item_desc'])[['预估操作时长', '预估医生写报告时长']].sum().reset_index()
    
    # 算宏观：计算历史中【每个特定星期几 + 特定小时】的大盘总耗时
    total_grp = df.groupby(['DayOfWeek', 'Hour'])[['预估操作时长', '预估医生写报告时长']].sum().reset_index()
    total_grp.rename(columns={'预估操作时长': '时段总技师历史耗时', '预估医生写报告时长': '时段总医生历史耗时'}, inplace=True)
    
    # 合并并计算比例 (指纹权重 W)
    profile = pd.merge(item_grp, total_grp, on=['DayOfWeek', 'Hour'])
    profile['技师耗时占比'] = profile['预估操作时长'] / profile['时段总技师历史耗时']
    profile['医生耗时占比'] = profile['预估医生写报告时长'] / profile['时段总医生历史耗时']
    profile = profile.fillna(0) # 防止除以0

    # ==========================================
    # 3. 运行 Prophet 进行大盘总量预测
    # ==========================================
    print(">>> 🔮 正在运行 Prophet 预测未来大盘总流量 (不惧怕稀疏矩阵)...")
    
    # 聚合到小时大盘
    hourly_total = df.groupby('ds')[['预估操作时长', '预估医生写报告时长']].sum().reset_index()
    
    # -- 技师总量模型 --
    df_tech = hourly_total[['ds', '预估操作时长']].rename(columns={'预估操作时长': 'y'})
    m_tech = Prophet(daily_seasonality=True, weekly_seasonality=True, yearly_seasonality=False)
    m_tech.fit(df_tech)
    future_tech = m_tech.make_future_dataframe(periods=forecast_days * 24, freq='H')
    fcst_tech = m_tech.predict(future_tech)
    fcst_tech['yhat'] = fcst_tech['yhat'].clip(lower=0) # 防止负数
    
    # -- 医生总量模型 --
    df_doc = hourly_total[['ds', '预估医生写报告时长']].rename(columns={'预估医生写报告时长': 'y'})
    m_doc = Prophet(daily_seasonality=True, weekly_seasonality=True, yearly_seasonality=False)
    m_doc.fit(df_doc)
    future_doc = m_doc.make_future_dataframe(periods=forecast_days * 24, freq='H')
    fcst_doc = m_doc.predict(future_doc)
    fcst_doc['yhat'] = fcst_doc['yhat'].clip(lower=0)

    # 合并未来预测大盘
    future_master = pd.DataFrame({
        '预测时间': fcst_tech['ds'],
        '预测_全科室技师总耗时': fcst_tech['yhat'],
        '预测_全科室医生总耗时': fcst_doc['yhat']
    })
    # 只提取未来的时间段（排除掉已经发生的历史）
    future_only = future_master[future_master['预测时间'] > df['ds'].max()].copy()

    # ==========================================
    # 4. 指纹下钻分配 (Top-Down 分发)
    # ==========================================
    print(">>> 🔪 正在进行微观项目降维拆解 (大饼切分)...")
    
    future_only['DayOfWeek'] = future_only['预测时间'].dt.dayofweek
    future_only['Hour'] = future_only['预测时间'].dt.hour
    
    # 将未来时间的星象（星期几、几点）与历史画像指纹进行碰撞匹配
    final_forecast = pd.merge(future_only, profile[['DayOfWeek', 'Hour', 'order_item_desc', '技师耗时占比', '医生耗时占比']], 
                              on=['DayOfWeek', 'Hour'], how='left')
    
    # 乘法公式落地：总数 * 比例 = 小项目精准数值
    final_forecast['预测该项目_技师操作时长(分)'] = final_forecast['预测_全科室技师总耗时'] * final_forecast['技师耗时占比']
    final_forecast['预测该项目_医生写报告时长(分)'] = final_forecast['预测_全科室医生总耗时'] * final_forecast['医生耗时占比']
    
    # 清理那些半夜预测出来的 0 耗时垃圾数据（保持表格干净）
    final_forecast = final_forecast[(final_forecast['预测该项目_技师操作时长(分)'] > 5) | 
                                    (final_forecast['预测该项目_医生写报告时长(分)'] > 5)]
    
    # 整理列排序，准备导出
    output_cols = ['预测时间', 'order_item_desc', 
                   '预测该项目_技师操作时长(分)', '预测该项目_医生写报告时长(分)', 
                   '预测_全科室技师总耗时', '预测_全科室医生总耗时']
    final_df = final_forecast[output_cols].sort_values(by=['预测时间', '预测该项目_技师操作时长(分)'], ascending=[True, False])

    output_csv = "Prophet_TopDown_Item_Forecast.csv"
    final_df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    
    print(f"\n🎉 降维打击成功！代码瞬间执行完毕！")
    print(f"📁 极度精细的小项目排班清单已生成: {output_csv}")

if __name__ == "__main__":
    run_prophet_topdown_forecast()