import os
import sys
import csv
import re
import time
import math
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Tuple

import requests
import pandas as pd
from PySide6 import QtCore, QtWidgets, QtGui


API_URL = "https://restapi.amap.com/v5/place/text"
DEFAULT_PAGE_SIZE = 25
DEFAULT_MAX_PAGES = 8  # 25 * 8 = 200
DEFAULT_SLEEP = 0.4


@dataclass
class Options:
    page_size: int
    max_pages: int
    sleep_seconds: float
    output_dir: str
    output_format: str  # csv | xlsx
    category_file: str


def parse_multi(value: str) -> List[str]:
    parts = re.split(r"[\s,;，；]+", value.strip())
    return [p.strip() for p in parts if p.strip()]


def normalize_typecode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        value = int(value)
    s = str(value).strip()
    if s.endswith(".0") and s.replace(".0", "").isdigit():
        s = s.replace(".0", "")
    if s.isdigit() and len(s) < 6:
        s = s.zfill(6)
    return s


def find_default_category_file() -> str:
    for name in os.listdir("."):
        if name.endswith(".xlsx") and ("POI" in name or "分类" in name):
            return os.path.abspath(name)
    return ""


def load_type_mapping(path: str) -> Dict[str, Tuple[str, str, str]]:
    if not path:
        return {}
    df = pd.read_excel(path, sheet_name=0)
    cols = df.columns.tolist()
    type_col = "NEW_TYPE" if "NEW_TYPE" in cols else None
    big_col = "大类" if "大类" in cols else None
    mid_col = "中类" if "中类" in cols else None
    sub_col = "小类" if "小类" in cols else None

    if not type_col:
        return {}

    mapping = {}
    for _, row in df.iterrows():
        t = normalize_typecode(row.get(type_col))
        if not t:
            continue
        big = str(row.get(big_col, "")).strip() if big_col else ""
        mid = str(row.get(mid_col, "")).strip() if mid_col else ""
        sub = str(row.get(sub_col, "")).strip() if sub_col else ""
        mapping[t] = (big, mid, sub)
    return mapping


class KeyPool:
    def __init__(self, keys: List[str]):
        self.keys = [k for k in keys if k]
        self.index = 0
        self.invalid = set()
        self.daily_limited = set()

    def has_available(self) -> bool:
        return any(k for k in self.keys if k not in self.invalid and k not in self.daily_limited)

    def current(self) -> str:
        if not self.keys:
            return ""
        return self.keys[self.index]

    def mark_invalid(self, key: str) -> None:
        if key:
            self.invalid.add(key)

    def mark_daily_limited(self, key: str) -> None:
        if key:
            self.daily_limited.add(key)

    def rotate(self) -> str:
        if not self.keys:
            return ""
        for _ in range(len(self.keys)):
            self.index = (self.index + 1) % len(self.keys)
            k = self.keys[self.index]
            if k not in self.invalid and k not in self.daily_limited:
                return k
        return ""


class Worker(QtCore.QThread):
    log = QtCore.Signal(str)
    progress = QtCore.Signal(int)
    preview = QtCore.Signal(list, list)
    finished = QtCore.Signal(str)
    failed = QtCore.Signal(str)
    need_key = QtCore.Signal(str)

    def __init__(self, keys, adcodes, types, options: Options):
        super().__init__()
        self.keys = keys
        self.adcodes = adcodes
        self.types = types
        self.options = options
        self._stop = False
        self.key_pool = None
        self._pause_mutex = QtCore.QMutex()
        self._pause_cond = QtCore.QWaitCondition()
        self._waiting_for_key = False
        self._session = requests.Session()
        # 去重集合：使用POI ID和坐标进行去重
        self.seen_poi_ids = set()
        self.seen_coordinates = set()

    def stop(self):
        self._stop = True
        self._pause_mutex.lock()
        self._pause_cond.wakeAll()
        self._pause_mutex.unlock()

    @staticmethod
    def _format_list(items: List[str], limit: int = 20) -> str:
        if not items:
            return ""
        if len(items) <= limit:
            return ", ".join(items)
        return ", ".join(items[:limit]) + f"...(+{len(items) - limit})"

    @staticmethod
    def _is_valid_poi(poi: dict, mapping: dict) -> bool:
        """验证POI数据的有效性"""
        # 检查必要字段
        poi_name = poi.get("name", "").strip()
        location = poi.get("location", "").strip()

        # 名称和坐标不能为空
        if not poi_name or not location or "," not in location:
            return False

        # 坐标必须是有效的经纬度格式
        try:
            lon, lat = location.split(",", 1)
            lon_f = float(lon)
            lat_f = float(lat)
            # 中国大陆经纬度范围粗略验证
            if not (73 <= lon_f <= 136 and 3 <= lat_f <= 54):
                return False
        except (ValueError, AttributeError):
            return False

        # 检查是否有分类信息（大中小类至少有一个不为空）
        typecode = poi.get("typecode", "")
        t_code = normalize_typecode(typecode)
        if t_code and t_code in mapping:
            big, mid, sub = mapping.get(t_code, ("", "", ""))
            if big or mid or sub:
                return True

        # 如果没有分类映射，但有typecode也认为有效
        if t_code:
            return True

        return False

    def add_keys(self, keys: List[str]) -> int:
        if not keys:
            return 0
        self._pause_mutex.lock()
        try:
            if not self.key_pool:
                return 0
            added = 0
            updated = 0
            for key in keys:
                cleared = False
                if key in self.key_pool.invalid:
                    self.key_pool.invalid.discard(key)
                    cleared = True
                if key in self.key_pool.daily_limited:
                    self.key_pool.daily_limited.discard(key)
                    cleared = True
                if key not in self.key_pool.keys:
                    self.key_pool.keys.append(key)
                    added += 1
                elif cleared:
                    updated += 1
            if self.key_pool.keys and self.key_pool.index >= len(self.key_pool.keys):
                self.key_pool.index = 0
            if self._waiting_for_key:
                self._pause_cond.wakeAll()
        finally:
            self._pause_mutex.unlock()
        if added and updated:
            self.log.emit(f"已补充 {added} 个Key，已更新 {updated} 个Key")
        elif added:
            self.log.emit(f"已补充 {added} 个Key，继续爬取")
        elif updated:
            self.log.emit("Key已更新，继续爬取")
        else:
            self.log.emit("未新增Key")
        return added

    def _wait_for_keys(self, reason: str) -> bool:
        if self._stop:
            return False
        if not self._waiting_for_key:
            self._waiting_for_key = True
            self.log.emit(f"{reason}，等待补充Key")
            self.need_key.emit(reason)
        self._pause_mutex.lock()
        try:
            while not self._stop and self.key_pool and not self.key_pool.has_available():
                self._pause_cond.wait(self._pause_mutex, 1000)
        finally:
            self._pause_mutex.unlock()
        if self._stop:
            return False
        if self.key_pool and self.key_pool.has_available():
            self._waiting_for_key = False
            return True
        return False

    def run(self):
        try:
            if not self.keys:
                self.failed.emit("API Key 不能为空")
                return
            if not self.adcodes:
                self.failed.emit("adcode 不能为空")
                return
            if not self.types:
                self.failed.emit("类型(typecode)不能为空")
                return

            self.log.emit(f"发现 {len(self.keys)} 个Key")
            self.log.emit(f"城市编码 {len(self.adcodes)} 个: {self._format_list(self.adcodes)}")
            self.log.emit(f"类型 {len(self.types)} 个: {self._format_list(self.types)}")
            self.log.emit(
                f"输出: {self.options.output_dir} ({self.options.output_format})"
            )
            self.log.emit(
                "分页: page_size="
                f"{self.options.page_size} max_pages={self.options.max_pages} "
                f"sleep={self.options.sleep_seconds}s"
            )

            mapping = load_type_mapping(self.options.category_file)
            if mapping:
                self.log.emit(f"分类映射: {len(mapping)} 条")
            else:
                self.log.emit("分类映射: 未加载或为空")
            key_pool = KeyPool(self.keys)
            self.key_pool = key_pool

            out_name = time.strftime("poi_%Y%m%d_%H%M%S")
            csv_path = os.path.join(self.options.output_dir, f"{out_name}.csv")
            xlsx_path = os.path.join(self.options.output_dir, f"{out_name}.xlsx")

            headers = ["城市编码", "城市", "大类", "中类", "小类", "名称", "经度", "纬度"]

            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()

                total = len(self.adcodes) * len(self.types)
                done = 0
                preview_rows = deque(maxlen=200)
                total_saved = 0  # 总共保存的数据条数

                for ad_index, adcode in enumerate(self.adcodes, start=1):
                    self.log.emit(f"开始城市 {adcode} ({ad_index}/{len(self.adcodes)})")
                    for t_code in self.types:
                        if self._stop:
                            self.log.emit("已停止")
                            self.finished.emit(csv_path)
                            return

                        combo_index = done + 1
                        self.log.emit(
                            f"正在爬取 城市={adcode} 类型={t_code} ({combo_index}/{total})"
                        )
                        rows = self.fetch_pois(key_pool, adcode, t_code, mapping)
                        for row in rows:
                            writer.writerow(row)

                        total_saved += len(rows)
                        if rows:
                            preview_rows.extend(rows)
                        if rows:
                            self.preview.emit(list(preview_rows), headers)

                        done += 1
                        self.progress.emit(int(done / total * 100))
                        time.sleep(self.options.sleep_seconds)

            # 输出最终统计
            self.log.emit("=" * 60)
            self.log.emit(f"采集完成！共保存 {total_saved} 条有效数据")
            self.log.emit(f"去重POI ID数: {len(self.seen_poi_ids)}")
            self.log.emit(f"去重坐标数: {len(self.seen_coordinates)}")
            self.log.emit("=" * 60)

            if self.options.output_format == "xlsx":
                df = pd.read_csv(csv_path, encoding="utf-8-sig")
                df.to_excel(xlsx_path, index=False)
                self.finished.emit(xlsx_path)
            else:
                self.finished.emit(csv_path)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self._session.close()

    def fetch_pois(self, key_pool: KeyPool, adcode: str, typecode: str, mapping):
        rows = []
        page_size = self.options.page_size
        max_pages = self.options.max_pages

        # 统计信息
        total_fetched = 0
        duplicate_count = 0
        invalid_count = 0

        for page_num in range(1, max_pages + 1):
            if self._stop:
                break

            data = self.request_once(key_pool, adcode, typecode, page_size, page_num)
            if data is None:
                break

            pois = data.get("pois", []) or []
            if not pois:
                break

            total_fetched += len(pois)

            for poi in pois:
                # 1. 数据验证：检查POI是否有效
                if not self._is_valid_poi(poi, mapping):
                    invalid_count += 1
                    continue

                # 2. 去重：检查POI ID
                poi_id = poi.get("id", "").strip()
                if poi_id and poi_id in self.seen_poi_ids:
                    duplicate_count += 1
                    continue

                # 3. 去重：检查坐标
                location = poi.get("location", "")
                if location in self.seen_coordinates:
                    duplicate_count += 1
                    continue

                # 提取数据
                lon, lat = "", ""
                if "," in location:
                    lon, lat = location.split(",", 1)

                cityname = poi.get("cityname", "")
                poi_name = poi.get("name", "")
                t_code = normalize_typecode(poi.get("typecode", "")) or normalize_typecode(typecode)
                big, mid, sub = mapping.get(t_code, ("", "", ""))

                row = {
                    "城市编码": adcode,
                    "城市": cityname,
                    "大类": big,
                    "中类": mid,
                    "小类": sub,
                    "名称": poi_name,
                    "经度": lon,
                    "纬度": lat,
                }

                # 记录已见过的ID和坐标
                if poi_id:
                    self.seen_poi_ids.add(poi_id)
                if location:
                    self.seen_coordinates.add(location)

                rows.append(row)

            # 如果返回的数据少于page_size，说明已经是最后一页
            if len(pois) < page_size:
                break

        # 输出统计信息
        if total_fetched > 0:
            self.log.emit(
                f"  城市={adcode} 类型={typecode}: "
                f"获取{total_fetched}条, 有效{len(rows)}条, "
                f"去重{duplicate_count}条, 无效{invalid_count}条"
            )

        return rows

    def request_once(self, key_pool: KeyPool, adcode: str, typecode: str, page_size: int, page_num: int):
        while True:
            if self._stop:
                return None
            if not key_pool.has_available():
                if not self._wait_for_keys("所有Key不可用或已达上限"):
                    return None

            tries = 0
            while tries < max(1, len(key_pool.keys)):
                if self._stop:
                    return None
                key = key_pool.current()
                if not key:
                    if not self._wait_for_keys("无可用Key"):
                        return None
                    break

                params = {
                    "key": key,
                    "types": typecode,
                    "region": adcode,
                    "city_limit": "true",
                    "page_size": page_size,
                    "page_num": page_num,
                    "output": "json",
                }

                try:
                    resp = self._session.get(API_URL, params=params, timeout=15)
                    data = resp.json()
                except Exception as exc:
                    self.log.emit(f"请求异常: {exc}")
                    return None

                if data.get("status") == "1":
                    return data

                info = data.get("info", "")
                infocode = data.get("infocode", "")
                self.log.emit(f"Key异常: {info} ({infocode})")

                if infocode in {"10001", "10009"} or "INVALID_USER_KEY" in info or "USERKEY_PLAT_NOMATCH" in info:
                    key_pool.mark_invalid(key)
                elif infocode in {"10044"} or "USER_DAILY_QUERY_OVER_LIMIT" in info:
                    key_pool.mark_daily_limited(key)
                elif "USER_QPS_OVER_LIMIT" in info:
                    time.sleep(1.0)
                    tries += 1
                    continue
                else:
                    key_pool.rotate()
                    tries += 1
                    continue

                key_pool.rotate()
                tries += 1
            if key_pool.has_available():
                return None


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.setWindowTitle("高德POI采集")
        self.resize(1400, 800)
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        # 主布局：左右分割
        main_layout = QtWidgets.QHBoxLayout(central)
        main_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        main_layout.addWidget(main_splitter)

        # ========== 左侧面板：配置区域 ==========
        left_panel = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        left_layout.setContentsMargins(5, 5, 5, 5)

        # API Key 输入组
        key_group = QtWidgets.QGroupBox("API Key")
        key_layout = QtWidgets.QVBoxLayout(key_group)
        self.keys_edit = QtWidgets.QPlainTextEdit()
        self.keys_edit.setPlaceholderText("多个Key用换行或逗号分隔")
        self.keys_edit.setMaximumHeight(80)
        key_layout.addWidget(self.keys_edit)
        left_layout.addWidget(key_group)

        # adcode 和 typecode 水平排列
        code_layout = QtWidgets.QHBoxLayout()

        adcode_group = QtWidgets.QGroupBox("adcode (城市编码)")
        adcode_layout = QtWidgets.QVBoxLayout(adcode_group)
        self.adcode_edit = QtWidgets.QPlainTextEdit()
        self.adcode_edit.setPlaceholderText("多个adcode用换行或逗号分隔\n例如：450300")
        adcode_layout.addWidget(self.adcode_edit)
        code_layout.addWidget(adcode_group)

        type_group = QtWidgets.QGroupBox("类型 (typecode)")
        type_layout = QtWidgets.QVBoxLayout(type_group)
        self.type_edit = QtWidgets.QPlainTextEdit()
        self.type_edit.setPlaceholderText("多个typecode用换行或逗号分隔\n例如：010000")
        type_layout.addWidget(self.type_edit)
        code_layout.addWidget(type_group)

        left_layout.addLayout(code_layout, 1)  # 占据剩余空间

        # 分类文件设置
        cat_group = QtWidgets.QGroupBox("分类文件")
        cat_layout = QtWidgets.QVBoxLayout(cat_group)
        self.category_path = QtWidgets.QLineEdit()
        self.category_path.setText(find_default_category_file())
        self.category_path.setPlaceholderText("选择POI分类表 (xlsx)")
        cat_layout.addWidget(self.category_path)
        cat_btn_layout = QtWidgets.QHBoxLayout()
        self.btn_browse_cat = QtWidgets.QPushButton("选择分类表")
        self.btn_load_types = QtWidgets.QPushButton("从分类表加载typecode")
        cat_btn_layout.addWidget(self.btn_browse_cat)
        cat_btn_layout.addWidget(self.btn_load_types)
        cat_layout.addLayout(cat_btn_layout)
        left_layout.addWidget(cat_group)

        # 请求设置
        opts_group = QtWidgets.QGroupBox("请求设置")
        opts_grid = QtWidgets.QGridLayout(opts_group)
        self.page_size = QtWidgets.QSpinBox()
        self.page_size.setRange(1, 25)
        self.page_size.setValue(DEFAULT_PAGE_SIZE)
        self.max_pages = QtWidgets.QSpinBox()
        self.max_pages.setRange(1, 8)
        self.max_pages.setValue(DEFAULT_MAX_PAGES)
        self.sleep_seconds = QtWidgets.QDoubleSpinBox()
        self.sleep_seconds.setRange(0.0, 10.0)
        self.sleep_seconds.setSingleStep(0.1)
        self.sleep_seconds.setValue(DEFAULT_SLEEP)
        self.output_format = QtWidgets.QComboBox()
        self.output_format.addItems(["csv", "xlsx"])
        opts_grid.addWidget(QtWidgets.QLabel("page_size:"), 0, 0)
        opts_grid.addWidget(self.page_size, 0, 1)
        opts_grid.addWidget(QtWidgets.QLabel("max_pages:"), 0, 2)
        opts_grid.addWidget(self.max_pages, 0, 3)
        opts_grid.addWidget(QtWidgets.QLabel("sleep:"), 1, 0)
        opts_grid.addWidget(self.sleep_seconds, 1, 1)
        opts_grid.addWidget(QtWidgets.QLabel("format:"), 1, 2)
        opts_grid.addWidget(self.output_format, 1, 3)
        left_layout.addWidget(opts_group)

        # 输出目录
        out_group = QtWidgets.QGroupBox("输出目录")
        out_layout = QtWidgets.QHBoxLayout(out_group)
        self.output_dir = QtWidgets.QLineEdit(os.path.abspath("."))
        self.btn_browse_out = QtWidgets.QPushButton("选择")
        out_layout.addWidget(self.output_dir)
        out_layout.addWidget(self.btn_browse_out)
        left_layout.addWidget(out_group)

        # 操作按钮和进度条
        action_layout = QtWidgets.QHBoxLayout()
        self.btn_start = QtWidgets.QPushButton("开始采集")
        self.btn_start.setMinimumHeight(36)
        self.btn_stop = QtWidgets.QPushButton("停止")
        self.btn_stop.setMinimumHeight(36)
        self.btn_stop.setEnabled(False)
        action_layout.addWidget(self.btn_start)
        action_layout.addWidget(self.btn_stop)
        left_layout.addLayout(action_layout)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setValue(0)
        left_layout.addWidget(self.progress)

        main_splitter.addWidget(left_panel)

        # ========== 右侧面板：日志和数据预览 ==========
        right_panel = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(5, 5, 5, 5)

        # 上下分割：日志和表格
        right_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)

        # 日志区域
        log_group = QtWidgets.QGroupBox("运行日志")
        log_layout = QtWidgets.QVBoxLayout(log_group)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        log_layout.addWidget(self.log)
        right_splitter.addWidget(log_group)

        # 数据预览区域
        table_group = QtWidgets.QGroupBox("数据预览 (最近200条)")
        table_layout = QtWidgets.QVBoxLayout(table_group)
        self.table = QtWidgets.QTableView()
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        table_layout.addWidget(self.table)
        right_splitter.addWidget(table_group)
        self._preview_headers = []
        self._preview_resized = False

        # 设置右侧分割比例 (日志:表格 = 3:7)
        right_splitter.setSizes([200, 500])
        right_layout.addWidget(right_splitter)

        main_splitter.addWidget(right_panel)

        # 设置主分割比例 (左:右 = 2:3)
        main_splitter.setSizes([450, 750])

        # 连接信号
        self.btn_browse_cat.clicked.connect(self.select_category_file)
        self.btn_load_types.clicked.connect(self.load_types_from_file)
        self.btn_browse_out.clicked.connect(self.select_output_dir)
        self.btn_start.clicked.connect(self.start_worker)
        self.btn_stop.clicked.connect(self.stop_worker)

    def select_category_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择分类表", "", "Excel (*.xlsx)")
        if path:
            self.category_path.setText(path)

    def load_types_from_file(self):
        path = self.category_path.text().strip()
        if not path:
            QtWidgets.QMessageBox.warning(self, "提示", "请先选择分类表文件")
            return
        try:
            df = pd.read_excel(path, sheet_name=0)
            if "NEW_TYPE" not in df.columns:
                QtWidgets.QMessageBox.warning(self, "提示", "分类表中找不到 NEW_TYPE 列")
                return
            types = [normalize_typecode(v) for v in df["NEW_TYPE"].tolist()]
            types = [t for t in types if t]
            self.type_edit.setPlainText("\n".join(sorted(set(types))))
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "错误", str(exc))

    def select_output_dir(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择输出目录")
        if path:
            self.output_dir.setText(path)

    def append_log(self, msg: str):
        self.log.appendPlainText(msg)

    def _ensure_preview_model(self, headers: List[str]):
        if self.table.model() is None or self._preview_headers != headers:
            model = QtGui.QStandardItemModel(0, len(headers))
            model.setHorizontalHeaderLabels(headers)
            self.table.setModel(model)
            self._preview_headers = list(headers)
            self._preview_resized = False
        return self.table.model()

    def update_preview(self, rows: List[dict], headers: List[str]):
        model = self._ensure_preview_model(headers)
        model.removeRows(0, model.rowCount())
        for row in rows:
            items = [QtGui.QStandardItem(str(row.get(h, ""))) for h in headers]
            model.appendRow(items)
        if rows and not self._preview_resized:
            self.table.resizeColumnsToContents()
            self._preview_resized = True

    def start_worker(self):
        keys = parse_multi(self.keys_edit.toPlainText())
        adcodes = parse_multi(self.adcode_edit.toPlainText())
        types = [normalize_typecode(t) for t in parse_multi(self.type_edit.toPlainText())]
        types = [t for t in types if t]

        options = Options(
            page_size=int(self.page_size.value()),
            max_pages=int(self.max_pages.value()),
            sleep_seconds=float(self.sleep_seconds.value()),
            output_dir=self.output_dir.text().strip() or os.path.abspath("."),
            output_format=self.output_format.currentText(),
            category_file=self.category_path.text().strip(),
        )

        self.worker = Worker(keys, adcodes, types, options)
        self.worker.log.connect(self.append_log)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.preview.connect(self.update_preview)
        self.worker.finished.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.need_key.connect(self.on_need_key)

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)
        self.worker.start()

    def stop_worker(self):
        if self.worker:
            self.worker.stop()
            self.append_log("正在停止...")

    def on_finished(self, path: str):
        self.append_log(f"完成，输出文件: {path}")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def on_failed(self, msg: str):
        self.append_log(f"失败: {msg}")
        QtWidgets.QMessageBox.critical(self, "错误", msg)
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def on_need_key(self, reason: str):
        if not self.worker:
            return
        msg = f"{reason}，请补充新的Key后继续："
        text, ok = QtWidgets.QInputDialog.getMultiLineText(self, "补充Key", msg, "")
        if not ok:
            self.append_log("已取消补充Key，任务停止")
            self.worker.stop()
            return
        new_keys = parse_multi(text)
        if not new_keys:
            self.append_log("未输入Key，继续等待补充")
            return
        self.worker.add_keys(new_keys)
        existing = self.keys_edit.toPlainText().strip()
        if existing:
            self.keys_edit.setPlainText(existing + "\n" + "\n".join(new_keys))
        else:
            self.keys_edit.setPlainText("\n".join(new_keys))


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
