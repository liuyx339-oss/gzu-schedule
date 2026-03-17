import pandas as pd

def clean_and_format_data(input_file="Radiology_Executed_Detail_updated_310.csv", 
                          output_file="Updated_Cleaned_Demand_Details.csv"):
    print(f">>> 🚀 启动数据清洗引擎...")
    print(f"读取原始数据: {input_file}")
    
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

    # 1. 筛选需要的核心列
    core_columns = [
        'eps_dept_desc', 'Type', '大分类', 
        '预估操作时长', '预估医生写报告时长', '总时长', '影像医生参与时长', 
        'order_item_desc', 'arrived_datetime'
    ]
    
    # 检查所有列是否存在，存在的才提取
    available_cols = [col for col in core_columns if col in df.columns]
    df_cleaned = df[available_cols].copy()

    print(">>> 🧹 正在清理时间戳与缺失值...")
    # 2. 清理抵达时间 (arrived_datetime)
    df_cleaned = df_cleaned.dropna(subset=['arrived_datetime'])
    df_cleaned['arrived_datetime'] = pd.to_datetime(df_cleaned['arrived_datetime'], errors='coerce')
    df_cleaned = df_cleaned.dropna(subset=['arrived_datetime'])

    print(">>> ⏱️ 正在派生时间与科室特征列...")
    # 3. 派生时间特征列 (对齐目标格式)
    df_cleaned['Date'] = df_cleaned['arrived_datetime'].dt.date
    df_cleaned['Hour'] = df_cleaned['arrived_datetime'].dt.hour
    df_cleaned['DayOfWeek'] = df_cleaned['arrived_datetime'].dt.dayofweek + 1

    # 4. 派生科室属性标识列 (Is_Checkup, Is_OBGYN)
    df_cleaned['Is_Checkup'] = df_cleaned['eps_dept_desc'].fillna('').str.contains('Health Management', case=False, na=False)
    df_cleaned['Is_OBGYN'] = df_cleaned['eps_dept_desc'].fillna('').str.contains('OBGYN', case=False, na=False)

    # 5. 排序与重置索引
    df_cleaned = df_cleaned.sort_values(by='arrived_datetime').reset_index(drop=True)

    final_columns = [
        'eps_dept_desc', 'Type', '大分类', '预估操作时长', '预估医生写报告时长', 
        '总时长', '影像医生参与时长', 'order_item_desc', 'arrived_datetime', 
        'Date', 'Hour', 'DayOfWeek', 'Is_Checkup', 'Is_OBGYN'
    ]
    df_final = df_cleaned[final_columns]

    # 6. 导出文件 (强制统一写回标准的 utf-8-sig，避免后续代码再报错)
    df_final.to_csv(output_file, index=False, encoding="utf-8-sig")
    print(f"✅ 数据清洗完成！")
    print(f"✅ 已成功转换 {len(df_final)} 条有效数据，并保存为: {output_file}")

if __name__ == "__main__":
    clean_and_format_data()