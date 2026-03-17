import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from prophet import Prophet
import warnings
import logging

# 屏蔽 prophet 的啰嗦日志
logging.getLogger('prophet').setLevel(logging.ERROR)
warnings.filterwarnings("ignore")

def get_clinical_cohort(row):
    """
    根据需求文档，将全院项目精准切分为 5 大独立特征阵营
    """
    desc = str(row['order_item_desc']).lower()
    cat = str(row['大分类'])
    type_val = str(row['Type'])
    
    if  type_val == 'CT':
        return 'CT_阵营'
    elif 'tte' in desc or 'echo' in desc or '心动' in desc:
        return 'TTE_阵营'
    elif type_val == '移动B超机：床边、术中、穿刺、消融' :
        return '超声介入_阵营'
    elif cat == '放射':
        return '常规放射_阵营'
    else:
        return '常规超声_阵营'

def run_prophet_federated_forecast(input_file="Updated_Cleaned_Demand_Details.csv", forecast_days=30):
    print(f"\n{'='*65}")
    print(f"🚀 启动 Prophet [联邦多阵营] 独立预测引擎 (增加层级字段)")
    print(f"{'='*65}")
    
    try:
        # 尝试用 UTF-8 读取
        df = pd.read_csv(input_file, encoding="utf-8-sig", low_memory=False)
    except UnicodeDecodeError:
        # 如果报错，说明是 Windows 导出的 GBK 编码，自动切换
        print(">>> ⚠️ UTF-8 解码失败，正在自动切换至 GBK 编码读取...")
        try:
            df = pd.read_csv(input_file, encoding="gbk", low_memory=False)
        except Exception as e:
            print(f"❌ GBK 读取也失败了: {e}")
            return
    except Exception as e:
        print(f"❌ 读取数据失败: {e}")
        return

    print(">>> 🧹 正在下钻数据并进行【五大阵营】特征分群...")
    df = df.dropna(subset=['arrived_datetime'])
    df['arrived_datetime'] = pd.to_datetime(df['arrived_datetime'], errors='coerce')
    df = df.dropna(subset=['arrived_datetime']).copy()
    
    df['ds'] = df['arrived_datetime'].dt.floor('H')
    df['Cohort'] = df.apply(get_clinical_cohort, axis=1)
    
    cohorts = df['Cohort'].unique()
    print(f"✅ 成功划定 {len(cohorts)} 个特征阵营: {', '.join(cohorts)}")
    
    all_item_forecasts = []
    
    for cohort in cohorts:
        print(f"\n>>> ⏳ 正在对【{cohort}】进行独立 AI 建模分析...")
        
        # 🚨 医疗常识注入：判断是否属于超声科系（无技师）
        is_ultrasound = cohort in ['TTE_阵营', '超声介入_阵营', '常规超声_阵营']
        if is_ultrasound:
            print("    💡 识别为超声科系，触发规则：技师工作量强行锁零，仅预测医生工作量。")

        cohort_df = df[df['Cohort'] == cohort]
        
        item_dur = cohort_df.groupby('order_item_desc')[['预估操作时长', '预估医生写报告时长']].sum()
        t_total = item_dur['预估操作时长'].sum()
        d_total = item_dur['预估医生写报告时长'].sum()
        
        item_props = pd.DataFrame()
        item_props['tech_prop'] = item_dur['预估操作时长'] / t_total if t_total > 0 else 0
        item_props['doc_prop'] = item_dur['预估医生写报告时长'] / d_total if d_total > 0 else 0
        item_props = item_props.reset_index()

        hourly_data = cohort_df.groupby('ds')[['预估操作时长', '预估医生写报告时长']].sum().reset_index()
        full_time_range = pd.date_range(start=hourly_data['ds'].min(), end=hourly_data['ds'].max(), freq='H')
        hourly_data = hourly_data.set_index('ds').reindex(full_time_range).fillna(0).reset_index().rename(columns={'index': 'ds'})

        # ---- 训练医生报告时长模型 (所有人都有) ----
        m_doc = Prophet(daily_seasonality=True, weekly_seasonality=True, yearly_seasonality='auto', changepoint_prior_scale=0.05)
        m_doc.add_country_holidays(country_name='CN')
        m_doc.fit(hourly_data[['ds', '预估医生写报告时长']].rename(columns={'预估医生写报告时长': 'y'}))
        fut = m_doc.make_future_dataframe(periods=forecast_days*24, freq='H')
        fcst_doc = m_doc.predict(fut)[['ds', 'yhat']].rename(columns={'yhat': 'pred_doc'})
        fcst_doc['pred_doc'] = fcst_doc['pred_doc'].clip(lower=0)
        
        # ---- 训练技师时长模型 (仅放射科系有) ----
        if not is_ultrasound:
            m_tech = Prophet(daily_seasonality=True, weekly_seasonality=True, yearly_seasonality='auto', changepoint_prior_scale=0.05)
            m_tech.add_country_holidays(country_name='CN')
            m_tech.fit(hourly_data[['ds', '预估操作时长']].rename(columns={'预估操作时长': 'y'}))
            fcst_tech = m_tech.predict(fut)[['ds', 'yhat']].rename(columns={'yhat': 'pred_tech'})
            fcst_tech['pred_tech'] = fcst_tech['pred_tech'].clip(lower=0)
        else:
            fcst_tech = pd.DataFrame({'ds': fut['ds'], 'pred_tech': 0})
        
        # 合并预测
        fcst = pd.merge(fcst_tech, fcst_doc, on='ds')
        fcst = fcst[fcst['ds'] > hourly_data['ds'].max()].copy()
        
        # ---- Top-Down 分摊与极致去噪 ----
        fcst['key'] = 1
        item_props['key'] = 1
        merged = pd.merge(fcst, item_props, on='key').drop('key', axis=1)
        
        merged['预测该项目_技师操作时长(分)'] = (merged['pred_tech'] * merged['tech_prop']).round(2)
        merged['预测该项目_医生写报告时长(分)'] = (merged['pred_doc'] * merged['doc_prop']).round(2)
        merged['预测时间'] = merged['ds']
        
        merged = merged[(merged['预测该项目_技师操作时长(分)'] > 0.01) | (merged['预测该项目_医生写报告时长(分)'] > 0.01)]
        
        all_item_forecasts.append(merged[['预测时间', 'order_item_desc', '预测该项目_技师操作时长(分)', '预测该项目_医生写报告时长(分)']])

    # ========================================================
    # 3. 组装与输出 (加入大类和大项目名称)
    # ========================================================
    print("\n>>> 💾 正在组装无噪数据并计算科室总负荷...")
    final_df = pd.concat(all_item_forecasts, ignore_index=True)
    
    # 🚨 新增：提取项目的归属映射字典 (大分类、Type、Cohort)
    # 🚨 新增：提取项目的归属映射字典 (强制小项目绝对唯一，过滤脏数据)
    item_mapping = df[['order_item_desc', '大分类', 'Type', 'Cohort']].drop_duplicates(subset=['order_item_desc']).set_index('order_item_desc')
    
    final_df['大分类'] = final_df['order_item_desc'].map(item_mapping['大分类']).fillna('未知科室')
    final_df['大项目(Type)'] = final_df['order_item_desc'].map(item_mapping['Type']).fillna('未知Type')
    final_df['模型阵营'] = final_df['order_item_desc'].map(item_mapping['Cohort']).fillna('未知阵营')
    
    dept_totals = final_df.groupby(['预测时间', '大分类'])[['预测该项目_技师操作时长(分)', '预测该项目_医生写报告时长(分)']].sum().reset_index()
    dept_totals = dept_totals.rename(columns={
        '预测该项目_技师操作时长(分)': '预测_全科室技师总耗时',
        '预测该项目_医生写报告时长(分)': '预测_全科室医生总耗时'
    })
    
    final_df = pd.merge(final_df, dept_totals, on=['预测时间', '大分类'])
    
    # 重排列表头，让层级从大到小显示，极其方便 Excel 筛选
    final_cols = [
        '预测时间', '大分类', '大项目(Type)', '模型阵营', 'order_item_desc', 
        '预测该项目_技师操作时长(分)', '预测该项目_医生写报告时长(分)', 
        '预测_全科室技师总耗时', '预测_全科室医生总耗时'
    ]
    final_df = final_df[final_cols].sort_values(['预测时间', '大分类', '大项目(Type)', 'order_item_desc'])
    
    output_csv = "Prophet_TopDown_Item_Forecast.csv"
    final_df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    print(f"✅ 数据瘦身完成！全新增加大项目层级的预测结果已保存至: {output_csv}")

if __name__ == "__main__":
    run_prophet_federated_forecast()