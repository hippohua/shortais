"""用东方财富 datacenter 获取全部A股代码（去重） + 腾讯行情"""
import requests, urllib3, time
urllib3.disable_warnings()

def get_all_a_stock_codes():
    """通过东方财富 datacenter 获取全部A股代码"""
    codes = set()
    page = 1
    page_size = 500
    
    while True:
        params = {
            'reportName': 'RPT_LICO_FN_CPD',
            'columns': 'SECURITY_CODE,SECURITY_NAME_ABBR',
            'pageNumber': page,
            'pageSize': page_size,
            'source': 'WEB',
            'client': 'WEB',
            'sortTypes': '1',
            'sortColumns': 'SECURITY_CODE',
        }
        try:
            r = requests.get(
                'https://datacenter-web.eastmoney.com/api/data/v1/get',
                params=params,
                headers={'Referer': 'https://data.eastmoney.com/'},
                timeout=15, verify=False
            )
            d = r.json()
            if not d.get('success') or not d.get('result'):
                break
            
            items = d['result'].get('data', [])
            if not items:
                break
            
            for item in items:
                code = item.get('SECURITY_CODE', '')
                if code:
                    codes.add(code)
            
            print(f'  page {page}: got {len(items)} items, unique total: {len(codes)}')
            
            # 如果返回数量少于page_size，说明到最后一页了
            if len(items) < page_size:
                break
            
            page += 1
            time.sleep(0.2)
        except Exception as e:
            print(f'  page {page} error: {e}')
            break
    
    return sorted(codes)

# 测试获取
print('Fetching all A stock codes...')
codes = get_all_a_stock_codes()
print(f'\nTotal unique codes: {len(codes)}')
print(f'Sample: {codes[:10]}')
print(f'Sample: {codes[-10:]}')

# 现在用腾讯接口测试获取行情
if codes:
    print(f'\nTesting Tencent quote for first 100 codes...')
    sample = codes[:100]
    tc_codes = []
    for c in sample:
        if c.startswith(('6', '9')):
            tc_codes.append(f'sh{c}')
        else:
            tc_codes.append(f'sz{c}')
    
    url = 'https://qt.gtimg.cn/q=' + ','.join(tc_codes[:50])  # 50只一批
    try:
        r = requests.get(url, headers={'Referer': 'https://gu.qq.com/'}, timeout=15, verify=False)
        r.encoding = 'gbk'
        lines = [l for l in r.text.strip().split('\n') if l.startswith('v_')]
        print(f'Got {len(lines)} quote lines')
        for line in lines[:3]:
            print(f'  {line[:120]}')
    except Exception as e:
        print(f'Tencent error: {e}')
