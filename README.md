# 高德POI采集工具

## 功能特点

- ✅ 支持多Key轮换
- ✅ 自动去重（POI ID + 坐标）
- ✅ 数据验证（名称、坐标、经纬度范围）
- ✅ 实时统计（获取/有效/去重/无效）
- ✅ PySide6可视化界面
- ✅ 支持CSV/XLSX导出

## 使用说明

1. 填写高德API Key
2. 输入城市编码（adcode）
3. 输入POI类型编码（typecode）
4. 点击"开始采集"

## 数据质量保证

- POI ID去重
- 坐标去重
- 空值过滤
- 经纬度范围验证（中国大陆）

## 技术栈

- Python 3.x
- PySide6
- Requests
- Pandas