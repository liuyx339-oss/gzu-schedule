import pandas as pd

def clean_and_extract_demand_data(file_path, sheet_name='data'):
    print(f">>> 正在读取 Excel 文件 '{file_path}' 中的 '{sheet_name}' 工作表...")
    
    try:
        # 使用 read_excel 并指定 sheet_name
        df = pd.read_excel(file_path, sheet_name=sheet_name)
    except Exception as e:
        print(f"❌ 读取文件失败，请检查文件名和工作表名称是否正确。错误信息: {e}")
        return None
    
    # ==========================================
    # 1. 严格按照您提供的橙色表头字段进行提取
    # ==========================================
    target_columns = [
        'eps_dept_desc',          # 科室
        'Type',                   # 影像类型细分 (CT, X-rays, Ultrasound等)
        '大分类',                 # 大分类 (放射 / 超声)
        '预估操作时长',           # 预估操作时长
        '预估医生写报告时长',     # 预估医生写报告时长
        '总时长',                 # 总时长
        '影像医生参与时长',       # 影像医生参与时长
        'order_item_desc',        # 具体检查项目描述
        'order_exec_datetime'     # 医嘱执行时间 (核心时间轴)
    ]
    
    # 检查列是否存在
    existing_columns = [col for col in target_columns if col in df.columns]
    missing_columns = [col for col in target_columns if col not in df.columns]
    
    if missing_columns:
        print(f"⚠️ 警告：未在 '{sheet_name}' 表中找到以下列，请核对名称是否有前后空格: {missing_columns}")
        
    df_extracted = df[existing_columns].copy()
    print(f">>> 成功提取 {len(existing_columns)} 个核心特征列。")

    # ==========================================
    # 2. 数据清洗与时间特征工程 (提取年月、星期、小时)
    # ==========================================
    if 'order_exec_datetime' in df_extracted.columns:
        # 转换为时间对象，无法转换的变为 NaT 后剔除
        df_extracted['order_exec_datetime'] = pd.to_datetime(df_extracted['order_exec_datetime'], errors='coerce')
        df_extracted = df_extracted.dropna(subset=['order_exec_datetime'])
        
        # 提取多维度的时间特征
        df_extracted['Date'] = df_extracted['order_exec_datetime'].dt.date         # 日期
        df_extracted['Hour'] = df_extracted['order_exec_datetime'].dt.hour         # 小时 (0-23)
        df_extracted['DayOfWeek'] = df_extracted['order_exec_datetime'].dt.dayofweek + 1 # 星期几 (1=周一)
    else:
        print("❌ 致命错误：缺少核心时间列 'order_exec_datetime'，无法生成热力图！")
        return None

    # 标记体检与妇科人群
    if 'eps_dept_desc' in df_extracted.columns:
        df_extracted['Is_Checkup'] = df_extracted['eps_dept_desc'].astype(str).str.contains('Health Management|Checkup', case=False, na=False)
        df_extracted['Is_OBGYN'] = df_extracted['eps_dept_desc'].astype(str).str.contains('OBGYN|Gynecology', case=False, na=False)

    # 保存清洗后的轻量化明细表
    detail_output_file = "Cleaned_Demand_Details.csv"
    df_extracted.to_csv(detail_output_file, index=False, encoding='utf-8-sig')
    print(f">>> 明细数据清洗完成，已保存为: {detail_output_file}")

    # ==========================================
    # 3. 聚合生成“每小时/周几”的流量热力图数据集
    # ==========================================
    print(">>> 正在生成流量热力图聚合数据...")
    
    # 按 大分类(放射/B超)、星期几、小时 聚合，统计患者数量
    heatmap_df = df_extracted.groupby(['大分类', 'DayOfWeek', 'Hour']).size().reset_index(name='Patient_Count')
    
    # --- 放射科热力图透视表 ---
    rad_heatmap = heatmap_df[heatmap_df['大分类'] == '放射'].pivot_table(
        index='Hour', 
        columns='DayOfWeek', 
        values='Patient_Count', 
        fill_value=0
    )
    if not rad_heatmap.empty:
        rad_heatmap_output = "Heatmap_Radiology_Hourly.csv"
        rad_heatmap.to_csv(rad_heatmap_output, encoding='utf-8-sig')
        print(f"✅ 放射科热力图数据已生成: {rad_heatmap_output}")
    
    # --- 超声科热力图透视表 ---
    us_heatmap = heatmap_df[heatmap_df['大分类'] == '超声'].pivot_table(
        index='Hour', 
        columns='DayOfWeek', 
        values='Patient_Count', 
        fill_value=0
    )
    if not us_heatmap.empty:
        us_heatmap_output = "Heatmap_Ultrasound_Hourly.csv"
        us_heatmap.to_csv(us_heatmap_output, encoding='utf-8-sig')
        print(f"✅ 超声科热力图数据已生成: {us_heatmap_output}")

if __name__ == "__main__":
    # 准确指定您本地的 Excel 文件名
    excel_file_name = "Radiology_Executed_Detail-ds_Detail_25.11.xlsx"
    # 准确指定数据所在的 Sheet 名
    target_sheet = "data" 
    
    clean_and_extract_demand_data(excel_file_name, sheet_name=target_sheet)