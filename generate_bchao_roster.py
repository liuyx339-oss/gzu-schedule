import pandas as pd

def generate_bchao_roster():
    print(">>> 启动【B超科】专属排班引擎...")
    
    # 参与动态排班的 4 位主力医生
    doctors = ["Xu Jing", "Liu Xiaoyan", "Lu Liyu", "Zhao Tianchong"]
    
    # 根据您的业务规则，设定每天所需的医生坑位数量
    # 1/3/5/6 体检（2人）；2/4/7 妇产科（3人）
    daily_req = {
        "周一": 2, "周二": 3, "周三": 2, "周四": 3, 
        "周五": 2, "周六": 2, "周日": 3
    }
    
    # 初始化本周工时池
    staff_status = {name: {"hours": 0.0} for name in doctors}
    roster_records = []
    days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    
    for day in days:
        req_num = daily_req[day]
        
        # 始终让当前本周排班工时最少的人优先排班，确保绝对公平
        doctors.sort(key=lambda x: staff_status[x]["hours"])
        
        for i in range(req_num):
            doc = doctors[i]
            # 智能班次分配：
            # 如果是妇产科高峰（需要3人），给部分人排偏下午的班次 (D2: 9:00-17:30)
            shift = "D (白班 8.5h)" if req_num == 2 else "D2 (晚白班 8h)"
            hours = 8.5 if req_num == 2 else 8.0
            
            roster_records.append({"日期": day, "姓名": doc, "角色": "B-Ultrasound Doctor", "班次": shift})
            staff_status[doc]["hours"] += hours
            
    # ==========================================
    # 插入主任 (李安华) 的固定特需门诊
    # ==========================================
    roster_records.append({"日期": "周一", "姓名": "Li Anhua (主任)", "角色": "Chief Doctor", "班次": "H3 (专家门诊 13:30-17:30)"})
    roster_records.append({"日期": "周四", "姓名": "Li Anhua (主任)", "角色": "Chief Doctor", "班次": "H3 (专家门诊 13:30-17:30)"})
    
    # 转置并生成 CSV
    df = pd.DataFrame(roster_records)
    pivot = df.pivot_table(index=['角色', '姓名'], columns='日期', values='班次', aggfunc='first').fillna("")
    pivot = pivot.reindex(columns=days)
    
    # 附加计算出的动态工时（主任固定为每周 8 小时）
    pivot['本周排班工时(H)'] = [staff_status.get(name, {"hours": 8.0})["hours"] for role, name in pivot.index]
    
    output_file = "Generated_Next_Week_Roster_Bchao.csv"
    pivot.to_csv(output_file, encoding='utf-8-sig')
    print(f"\n✅ 成功！B超专属排班表已生成: {output_file}")
    print(pivot.to_string())

if __name__ == "__main__":
    generate_bchao_roster()