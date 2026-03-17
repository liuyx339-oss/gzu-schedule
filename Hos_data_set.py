import pandas as pd
import numpy as np
import os
import glob

# 1. 定义班次映射字典
SHIFT_MAPPING = {
    "D":  {"start": "08:30", "end": "17:30", "duration": 8.5, "type": "Day"},
    "N":  {"start": "17:30", "end": "08:00", "duration": 14.5, "type": "Night"},
    "D1": {"start": "08:30", "end": "17:00", "duration": 8.0, "type": "Day"},
    "L/N":{"start": "08:00", "end": "08:00", "duration": 24.0, "type": "Long/Night"},
    "D2": {"start": "09:00", "end": "17:30", "duration": 8.0, "type": "Day"},
    "兼职夜班":{"start": "18:00", "end": "08:00", "duration": 14.0, "type": "Night"},
    "D3": {"start": "09:30", "end": "18:00", "duration": 8.0, "type": "Day"},
    "PTO":{"start": "08:00", "end": "17:00", "duration": 8.0, "type": "Leave"},
    "D4": {"start": "09:00", "end": "18:00", "duration": 8.5, "type": "Day"},
    "CTO":{"start": None,    "end": None,    "duration": 0.0, "type": "Leave"},
    "C":  {"start": "07:40", "end": "16:10", "duration": 8.0, "type": "Day"},
    "C1": {"start": "08:00", "end": "16:30", "duration": 8.0, "type": "Day"},
    "H1": {"start": "07:40", "end": "11:40", "duration": 4.0, "type": "Half-Day"},
    "H2": {"start": "08:30", "end": "12:30", "duration": 4.0, "type": "Half-Day"},
    "H3": {"start": "13:30", "end": "17:30", "duration": 4.0, "type": "Half-Day"},
    "T":  {"start": "08:00", "end": "12:00", "duration": 4.0, "type": "Half-Day"},
    "L":  {"start": "08:00", "end": "17:30", "duration": 9.0, "type": "Day"},
    "N2": {"start": "17:30", "end": "12:00", "duration": 18.5, "type": "Night"},
}

# 用于从 Sheet 名称提取月份
MONTH_MAP = {
    'JAN': '01', 'FEB': '02', 'MAR': '03', 'APR': '04', 'MAY': '05', 'JUN': '06',
    'JUL': '07', 'AUG': '08', 'SEP': '09', 'OCT': '10', 'NOV': '11', 'DEC': '12'
}

def process_single_excel(file_path, role, year=2025):
    """
    读取单个 Excel 文件中的所有 Sheet 并合并
    """
    print(f"\n>>> 正在处理 Excel 文件: {file_path} (角色: {role})")
    all_sheets_data = []
    
    # 获取 Excel 中所有的 Sheet 名称
    xls = pd.ExcelFile(file_path)
    sheet_names = xls.sheet_names
    
    for sheet in sheet_names:
        # 1. 识别当前 Sheet 对应的月份
        month_str = None
        sheet_upper = sheet.upper().strip()
        for key, val in MONTH_MAP.items():
            if key in sheet_upper:
                month_str = val
                break
        
        # 如果 Sheet 名字里找不到月份（比如是说明页），则跳过
        if not month_str:
            print(f"  - 跳过 Sheet: '{sheet}' (无法识别月份)")
            continue
            
        print(f"  - 正在解析 Sheet: '{sheet}' -> 对应月份: {month_str}")
        
        # 2. 读取当前 Sheet 的数据，跳过前两行复杂表头
        df = pd.read_excel(file_path, sheet_name=sheet, skiprows=2)
        
        if df.empty or len(df.columns) < 5:
            continue
            
        # 3. 数据清洗与提取
        name_col = df.columns[0] # 第一列通常是名字
        df = df.dropna(subset=[name_col])
        
        # 剔除无关行
        invalid_keywords = ['Radiology', 'NOTE', '休假备注', 'Regular', 'D1', 'D2', 'L/N', 'Dustin Huang', '136', '137', '159', 'ON CALL']
        df = df[~df[name_col].astype(str).str.contains('|'.join(invalid_keywords), case=False, na=False)]
        
        # 提取 1 到 31 号的列
        day_columns = [col for col in df.columns if str(col).strip().isdigit() and 1 <= int(str(col).strip()) <= 31]
        df_clean = df[[name_col] + day_columns].copy()
        df_clean.rename(columns={name_col: 'Name'}, inplace=True)
        
        # 4. 宽表变长表 (Melt)
        df_melted = df_clean.melt(id_vars=['Name'], var_name='Day', value_name='ShiftCode')
        
        # 过滤无效排班
        df_melted['ShiftCode'] = df_melted['ShiftCode'].astype(str).str.strip()
        df_melted = df_melted[df_melted['ShiftCode'].notna()]
        df_melted = df_melted[~df_melted['ShiftCode'].isin(['nan', '*', '0', ''])]
        
        # 5. 生成日期
        df_melted['DateStr'] = f"{year}-{month_str}-" + df_melted['Day'].astype(str)
        df_melted['Date'] = pd.to_datetime(df_melted['DateStr'], errors='coerce')
        df_melted = df_melted.dropna(subset=['Date']) # 删掉 2月30日 这种不存在的日期
        
        # 6. 匹配字典
        df_melted['Role'] = role
        def get_shift_info(code, key):
            clean_code = code.split('(')[0].split('（')[0].strip()
            return SHIFT_MAPPING.get(clean_code, {}).get(key, None)

        df_melted['Start_Time'] = df_melted['ShiftCode'].apply(lambda x: get_shift_info(x, 'start'))
        df_melted['End_Time']   = df_melted['ShiftCode'].apply(lambda x: get_shift_info(x, 'end'))
        df_melted['Duration_H'] = df_melted['ShiftCode'].apply(lambda x: get_shift_info(x, 'duration'))
        df_melted['Shift_Type'] = df_melted['ShiftCode'].apply(lambda x: get_shift_info(x, 'type'))

        final_cols = ['Date', 'Name', 'Role', 'ShiftCode', 'Shift_Type', 'Start_Time', 'End_Time', 'Duration_H']
        all_sheets_data.append(df_melted[final_cols])
        
    if all_sheets_data:
        return pd.concat(all_sheets_data, ignore_index=True)
    else:
        return pd.DataFrame()

# ================= 批量执行入口 =================
if __name__ == "__main__":
    all_roles_data = []
    
    # 自动寻找当前文件夹下的所有 xlsx 文件 (排除已经处理好的输出文件或带 ~$ 的临时隐藏文件)
    excel_files = [f for f in glob.glob("*.xlsx") if not f.startswith("~$")]
    
    if not excel_files:
        print("未在当前目录找到 Excel (.xlsx) 文件，请检查。")
    else:
        for file in excel_files:
            # 根据您的文件名习惯自动判断是医生还是技师
            # (如果文件名包含"radiology roster"且没有连在一起，算作医生)
            if "2025radiology" in file.replace(" ", "") and "tech" not in file.lower():
                # 注意：这里需要根据您真实的技师文件名稍微调整判断条件
                # 假设只要包含 roster 就是排班表
                role = "Technician" if "radiographer" in file.lower() or "2025radiology" in file.replace(" ", "") else "Doctor"
            else:
                role = "Doctor"
                
            df_file = process_single_excel(file, role)
            if not df_file.empty:
                all_roles_data.append(df_file)
                
        # 最终合并并输出
        if all_roles_data:
            master_df = pd.concat(all_roles_data, ignore_index=True)
            master_df = master_df.sort_values(by=['Date', 'Role', 'Name']).reset_index(drop=True)
            
            output_name = "Master_Roster_Database.csv"
            master_df.to_csv(output_name, index=False, encoding='utf-8-sig')
            print(f"\n✅ 成功！所有 Sheet 已合并清洗完毕。输出文件已保存为: {output_name}")
            print(f"总计提取到有效排班记录 {len(master_df)} 条。")