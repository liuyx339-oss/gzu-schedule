import pandas as pd
from datetime import datetime

def check_roster_compliance(roster_file="Master_Roster_Database.csv"):
    print(f">>> 正在读取排班数据库: {roster_file} ...")
    
    try:
        df = pd.read_csv(roster_file)
    except FileNotFoundError:
        print("❌ 找不到排班数据库文件！请确认之前是否成功生成了 Master_Roster_Database.csv")
        return

    # 过滤掉请假 (Leave) 的记录，只保留实际在岗的班次
    df_working = df[df['Shift_Type'] != 'Leave'].copy()
    
    # 按照日期分组，统计每天各班次的人数
    dates = sorted(df_working['Date'].unique())
    
    violations = []
    
    print(">>> 正在启动排班合理性扫描引擎...")
    
    for current_date in dates:
        # 获取当天所有在岗人员
        day_data = df_working[df_working['Date'] == current_date]
        
        # ----------------------------------------------------
        # 1. 统计白班人员 (包含白班 Day, 长白班 L/N, 半天班 Half-Day)
        # ----------------------------------------------------
        day_shifts = ['Day', 'Long/Night', 'Half-Day']
        day_staff = day_data[day_data['Shift_Type'].isin(day_shifts)]
        
        doc_day_count = len(day_staff[day_staff['Role'] == 'Doctor'])
        tech_day_count = len(day_staff[day_staff['Role'] == 'Technician'])
        
        # ----------------------------------------------------
        # 2. 统计夜班人员 (包含夜班 Night, 跨夜班 L/N)
        # ----------------------------------------------------
        night_shifts = ['Night', 'Long/Night']
        night_staff = day_data[day_data['Shift_Type'].isin(night_shifts)]
        
        doc_night_count = len(night_staff[night_staff['Role'] == 'Doctor'])
        # 题目未强制要求晚上技师数量，但可以顺便统计
        tech_night_count = len(night_staff[night_staff['Role'] == 'Technician'])
        
        # ----------------------------------------------------
        # 3. 冲突与违规判定逻辑 (核心业务规则)
        # ----------------------------------------------------
        issues = []
        is_violated = False
        
        # 规则 A：白天至少 1 医 2 技
        if doc_day_count < 1:
            issues.append(f"白班缺医生(仅{doc_day_count}人)")
            is_violated = True
        if tech_day_count < 2:
            issues.append(f"白班缺技师(仅{tech_day_count}人)")
            is_violated = True
            
        # 规则 B：晚上至少 1 医
        if doc_night_count < 1:
            issues.append(f"夜班缺医生(仅{doc_night_count}人)")
            is_violated = True
            
        # 额外软约束检测（周1/3/5/6体检高峰预警）
        # 将日期字符串转为datetime以判断星期几
        dt_obj = pd.to_datetime(current_date)
        day_of_week = dt_obj.dayofweek + 1 # 1=周一
        
        if day_of_week in [1, 3, 5, 6]: # 周1, 3, 5, 6 体检高峰
            if doc_day_count == 1:
                # 哪怕满足了最基础的1个人，但在高峰期依然非常危险
                issues.append("⚠️体检高峰日：白天仅排1名医生，面临爆单风险！")
                # 这算预警，可以不标记为致命违规，但记录下来
                
        # ----------------------------------------------------
        # 4. 记录违规详情
        # ----------------------------------------------------
        if is_violated or "⚠️" in str(issues):
            violations.append({
                "日期": current_date,
                "星期": f"周{day_of_week}",
                "白班医生数": doc_day_count,
                "白班技师数": tech_day_count,
                "夜班医生数": doc_night_count,
                "系统警告": " | ".join(issues)
            })

    # 输出检测报告
    if violations:
        violation_df = pd.DataFrame(violations)
        output_file = "Roster_Violations_Report.csv"
        violation_df.to_csv(output_file, index=False, encoding='utf-8-sig')
        
        print("\n================= 🚨 排班漏洞扫描结果 🚨 =================")
        print(f"总计扫描天数: {len(dates)} 天")
        print(f"发现异常天数: {len(violations)} 天")
        print(f"详细的“找茬”报告已生成: {output_file}")
        
        # 打印前 5 条严重违规在屏幕上看看
        print("\n【近期排班违规示例】：")
        print(violation_df[['日期', '星期', '系统警告']].head(10).to_string(index=False))
        print("========================================================\n")
    else:
        print("\n✅ 完美！历史排班表100%符合排班规则，未发现任何缺人漏洞！")

if __name__ == "__main__":
    check_roster_compliance()