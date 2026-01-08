import csv
import sys

def clean_csv(input_file, output_file):
    """清洗CSV数据：去除坐标相同的重复数据和大中小类均为空的数据"""

    print(f"正在读取文件: {input_file}")

    # 读取CSV文件
    with open(input_file, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    original_count = len(rows)
    print(f"原始数据行数: {original_count}")

    # 1. 去除大中小类均为空的数据
    print("\n步骤1: 去除大中小类均为空的数据...")
    filtered_rows = []
    empty_categories_count = 0

    for row in rows:
        big = row.get('大类', '').strip()
        mid = row.get('中类', '').strip()
        small = row.get('小类', '').strip()

        # 如果大中小类都为空，跳过这条数据
        if not big and not mid and not small:
            empty_categories_count += 1
            continue

        filtered_rows.append(row)

    print(f"  发现大中小类均为空的数据: {empty_categories_count} 条")
    print(f"  清洗后剩余: {len(filtered_rows)} 条")

    # 2. 去除坐标相同的重复数据
    print("\n步骤2: 去除坐标相同的重复数据...")
    before_dedup = len(filtered_rows)

    # 使用字典记录已出现的坐标
    seen_coords = {}
    deduped_rows = []

    for row in filtered_rows:
        lon = row.get('经度', '').strip()
        lat = row.get('纬度', '').strip()
        coord_key = f"{lon},{lat}"

        # 如果坐标未出现过，保留这条数据
        if coord_key not in seen_coords:
            seen_coords[coord_key] = True
            deduped_rows.append(row)

    duplicate_coords_count = before_dedup - len(deduped_rows)
    print(f"  发现坐标重复的数据: {duplicate_coords_count} 条")
    print(f"  清洗后剩余: {len(deduped_rows)} 条")

    # 保存清洗后的数据
    print(f"\n正在保存清洗后的数据到: {output_file}")

    if deduped_rows:
        fieldnames = list(deduped_rows[0].keys())
        with open(output_file, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(deduped_rows)

    # 统计报告
    print("\n" + "="*60)
    print("清洗完成！统计报告：")
    print("="*60)
    print(f"原始数据:           {original_count:>8} 条")
    print(f"大中小类均为空:     {empty_categories_count:>8} 条")
    print(f"坐标重复:           {duplicate_coords_count:>8} 条")
    print(f"清洗后数据:         {len(deduped_rows):>8} 条")
    print(f"删除总计:           {original_count - len(deduped_rows):>8} 条")
    print(f"保留比例:           {len(deduped_rows)/original_count*100:>7.2f}%")
    print("="*60)

if __name__ == "__main__":
    input_file = "2026.csv"
    output_file = "2026_cleaned.csv"

    try:
        clean_csv(input_file, output_file)
    except Exception as e:
        print(f"\n错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
