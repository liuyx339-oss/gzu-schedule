import os
import re

def bulletproof_patch(filename):
    if not os.path.exists(filename): 
        print(f"❌ 找不到文件: {filename}")
        return
        
    with open(filename, 'r', encoding='utf-8') as f:
        html = f.read()

    print(f">>> 🩺 正在对 {filename} 进行绝对安全手术...")

    # 1. 精准替换单选按钮 (严格限定只替换这一句)
    html = re.sub(
        r'<el-radio-group v-model="smartRosterType"[^>]*>.*?</el-radio-group>',
        '''<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
            <el-radio-group v-model="smartRosterType" size="large">
                <el-radio-button label="fixed">⚖️ 原固定排班表</el-radio-button>
                <el-radio-button label="predict">✨ 预测班次矩阵 (新)</el-radio-button>
            </el-radio-group>
            <div style="background: #f0f9eb; padding: 10px 20px; border-radius: 8px; border: 1px solid #e1f3d8; box-shadow: 0 2px 4px rgba(0,0,0,0.05);">
                <span style="margin-right: 15px; font-weight: bold; color: #67c23a;">⚙️ 管理员模式</span>
                <el-switch v-model="isEditMode" active-text="开启修改" inactive-text="锁定视图"></el-switch>
                <el-button v-if="isEditMode" type="primary" size="small" style="margin-left: 20px;" @click="saveEdits">
                    💾 确认修改并导出 CSV
                </el-button>
            </div>
        </div>''',
        html, count=1, flags=re.DOTALL
    )

    # 2. 表格克隆与下拉框注入 (精准锁定原有表格结构)
    table_match = re.search(r'(<el-table v-if="smartRosterType === \'fixed\'"[^>]*>.*?</el-table>)', html, flags=re.DOTALL)
    if table_match:
        fixed_table = table_match.group(1)
        
        # 注入下拉框
        cell_regex = r'<div[^>]*:class="getSmartShiftClass\(scope\.row\[([^\]]+)\]\)"[^>]*>\s*\{\{\s*scope\.row\[\1\]\s*\}\}\s*</div>'
        def cell_replacer(m):
            v = m.group(1)
            return f'''<div v-if="!isEditMode" :class="getSmartShiftClass(scope.row[{v}])">
                            {{{{ scope.row[{v}] }}}}
                        </div>
                        <el-select v-else v-model="scope.row[{v}]" size="small" style="width: 100%;" filterable placeholder="选择">
                            <el-option v-for="opt in shiftOptions" :key="opt" :label="opt" :value="opt"></el-option>
                        </el-select>'''
        
        fixed_table_edited = re.sub(cell_regex, cell_replacer, fixed_table)
        predict_table = fixed_table_edited.replace("smartRosterType === 'fixed'", "smartRosterType === 'predict'").replace(':data="smartFixedData"', ':data="smartPredictData"')
        
        # 删掉潮汐流水，拼接预测表格
        html = re.sub(r'<el-table v-if="smartRosterType === \'dynamic\'".*?</el-table>', '', html, flags=re.DOTALL)
        html = html.replace(fixed_table, fixed_table_edited + '\n\n' + predict_table)

    # 3. 核心 JS 变量安全注入
    js_hook = r"const smartRosterType = ref\('fixed'\);"
    js_inject = """const smartRosterType = ref('fixed');
    const smartPredictDataRaw = ref([]);
    const isEditMode = ref(false);
    const shiftOptions = [
        '休', 'Opt_D1(09:00-18:00)', 'Opt_D2(08:30-17:30)', 'Opt_D3(10:00-19:00)',
        'Opt_H1(09:00-13:00)', 'Opt_H2(14:00-18:00)', 'N(17:30-08:00)', 
        '兼职夜(18:00-08:00)', 'N(OnCall)(17:30-08:00)', 'H3(专家)(13:30-17:30)',
        'D1(08:30-17:00)', 'D(08:30-17:30)', 'D2(09:00-17:30)', 'D3(09:30-18:00)', 
        'D4(09:00-18:00)', 'D5(08:30-18:00)', 'C(07:40-16:10)', 'C1(08:00-16:30)',
        'H1(07:40-11:40)', 'H2(08:30-12:30)', 'T(08:00-12:00)', 'L/N(08:00-08:00)'
    ];
    const saveEdits = () => {
        const currentData = smartRosterType.value === 'fixed' ? smartFixedDataRaw.value : smartPredictDataRaw.value;
        const cols = ['姓名', '人员属性', '本期总工时'].concat(filteredSmartCols.value || []);
        let csvContent = '\\uFEFF' + cols.join(',') + '\\n';
        currentData.forEach(row => {
            let rowData = cols.map(c => {
                let cell = row[c] || '';
                if(String(cell).includes(',')) return `"${cell}"`;
                return cell;
            });
            csvContent += rowData.join(',') + '\\n';
        });
        const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
        const link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        let prefix = smartRosterType.value === 'fixed' ? 'Fixed' : 'Predict';
        link.download = `Modified_${prefix}_Shift_Matrix.csv`;
        link.click();
        isEditMode.value = false;
        alert('✅ 修改已生效！已为您下载 CSV。');
    };"""
    html = re.sub(js_hook, lambda m: js_inject, html, count=1)

    # 4. 数据绑定 (保证预测表格能读到 JSON)
    html = re.sub(r'smartFixedDataRaw\.value\s*=\s*data\.fixed\.data;', 
                  r'smartFixedDataRaw.value = data.fixed.data;\nif(data.predict) { smartPredictDataRaw.value = data.predict.data; }', html)

    comp_match = re.search(r'(const smartFixedData = computed\(\(\) => \{.*?\n\s*\}\);)', html, flags=re.DOTALL)
    if comp_match:
        fixed_comp = comp_match.group(1)
        predict_comp = fixed_comp.replace('smartFixedData', 'smartPredictData').replace('smartFixedDataRaw', 'smartPredictDataRaw')
        html = html.replace(fixed_comp, fixed_comp + '\n' + predict_comp)

    # 5. 暴力破解白屏的终极方案：强制在 return { 的第一行暴露所有变量
    html = re.sub(r'(return\s*\{)', r'\1 isEditMode, shiftOptions, saveEdits, smartPredictData, ', html, count=1)

    with open(filename, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"✅ {filename} 修复完毕！")

bulletproof_patch('index.html')
bulletproof_patch('index_bchao.html')
print("🎉 绝对安全版升级结束。")