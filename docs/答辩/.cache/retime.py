"""按实际字数重算解说稿每页的建议时长，并写回页眉与总表。"""
import re
import pathlib

RATE_CJK = 0.25      # 240 字/分钟
RATE_LAT = 0.45      # 英文单词
RATE_DIG = 0.28      # 数字逐个读

p = pathlib.Path(r'C:\Text2SQL\docs\答辩\演讲解说稿.md')
t = p.read_text(encoding='utf-8')

parts = t.split('\n## ')
head = parts[0]
secs = parts[1:]

rows = []          # (title, seconds, start, end)
new_secs = []
cursor = 0.0

for s in secs:
    lines = s.split('\n')
    lines[0] = lines[0].lstrip('# ').strip()
    m = re.match(r'^\*\*(\d\d)　(.+?)　[\d:]+ – [\d:]+\*\*$', lines[0])
    if not m:
        new_secs.append(s)
        continue
    num, title = m.group(1), m.group(2)
    body = [l for l in lines[1:]
            if l.strip() and l.strip() != '---' and not l.strip().startswith('*提示')]
    text = ''.join(body)
    cjk = len(re.findall(r'[\u4e00-\u9fff]', text))
    lat = len(re.findall(r'[A-Za-z][A-Za-z\.\-]*', text))
    dig = len(re.findall(r'[0-9]', text))
    dur = cjk * RATE_CJK + lat * RATE_LAT + dig * RATE_DIG
    start, cursor = cursor, cursor + dur
    rows.append((num, title, cjk, round(dur), start, cursor))
    lines[0] = '## **%s　%s　%s – %s**' % (
        num, title,
        '%d:%02d' % (int(start) // 60, int(start) % 60),
        '%d:%02d' % (int(cursor) // 60, int(cursor) % 60))
    new_secs.append('\n'.join(lines))

out = '\n'.join([head] + new_secs)


def mm(s):
    return '%d:%02d' % (int(s) // 60, int(s) % 60)


table_lines = ['| 页码 | 页面标题 | 建议时长 | 累计 |', '|---|---|---|---|']
for num, title, cjk, dur, _, end in rows:
    table_lines.append('| %s | %s | %d 秒 | %s |' % (num, title, dur, mm(end)))

out = re.sub(
    r'\| 页码 \| 页面标题 \| 建议时长 \| 累计 \|.*?\n\n---',
    '\n'.join(table_lines) + '\n\n---',
    out,
    flags=re.S)

p.write_text(out, encoding='utf-8')

print('| 页 | 汉字 | 秒 | 区间 |')
for num, title, cjk, dur, s0, s1 in rows:
    print('%s %s | %d | %d | %s – %s' % (num, title, cjk, dur, mm(s0), mm(s1)))
print('\n合计汉字 %d；正文 %s（约 %d 分 %d 秒）'
      % (sum(r[2] for r in rows),
         mm(rows[-1][5]), int(rows[-1][5]) // 60, int(rows[-1][5]) % 60))
