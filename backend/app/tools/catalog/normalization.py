"""Caller argument normalization; preserves the existing compatibility vocabulary."""
import re

from app.tools.catalog.fields import FILTER_FIELDS, canonical_field

CATEGORY_ALIASES = {
    "mouse": "鼠标",
    "mice": "鼠标",
    "鼠标": "鼠标",
    "游戏鼠标": "鼠标",
    "keyboard": "键盘",
    "keyboards": "键盘",
    "键盘": "键盘",
    "机械键盘": "键盘",
    "headphone": "耳机",
    "headphones": "耳机",
    "headset": "耳机",
    "headsets": "耳机",
    "earphone": "耳机",
    "earphones": "耳机",
    "耳机": "耳机",
    "耳麦": "耳机",
    "头戴耳机": "耳机",
    "游戏耳机": "耳机",
    "monitor": "显示器",
    "monitors": "显示器",
    "display": "显示器",
    "screen": "显示器",
    "显示器": "显示器",
    "屏幕": "显示器",
    "speaker": "音箱",
    "speakers": "音箱",
    "音箱": "音箱",
    "音响": "音箱",
    "蓝牙音箱": "音箱",
    "webcam": "摄像头",
    "webcams": "摄像头",
    "camera": "摄像头",
    "摄像头": "摄像头",
    "网络摄像头": "摄像头",
}

ALLOWED_CATEGORIES = {
    "mouse", "keyboard", "headset", "headphone", "monitor", "speaker", "webcam",
    *CATEGORY_ALIASES.values(),
}
ALLOWED_FILTERS = set(FILTER_FIELDS) - {"brand", "price", "stock"}

def _normalize_catalog_filters_with_debug(
    filters: dict[str, str],
) -> tuple[dict[str, str], dict[str, list[dict[str, dict[str, str]]]]]:
    normalized: dict[str, str] = {}
    changes: list[dict[str, dict[str, str]]] = []
    for raw_key, raw_value in filters.items():
        raw_key_text = str(raw_key)
        raw_value_text = str(raw_value)
        key = _normalize_filter_key(raw_key_text)
        value = _normalize_filter_value(key, raw_value_text)
        normalized[key] = value
        if key != raw_key_text or value != raw_value_text:
            changes.append(
                {
                    "from": {"key": raw_key_text, "value": raw_value_text},
                    "to": {"key": key, "value": value},
                }
            )
    debug = {"filter_aliases": changes} if changes else {}
    return normalized, debug


def _normalize_filter_key(key: str) -> str:
    return canonical_field(key)


def _normalize_filter_value(key: str, value: str) -> str:
    stripped = value.strip()
    lowered = stripped.lower()
    if key == "connection_type":
        exact_connection_aliases = {
            "usb-c": "USB-C",
            "usb c": "USB-C",
            "usbc": "USB-C",
            "type-c": "USB-C",
            "type c": "USB-C",
            "usb-a": "USB-A",
            "usb a": "USB-A",
            "usba": "USB-A",
            "type-a": "USB-A",
            "type a": "USB-A",
            "bluetooth": "蓝牙",
            "bt": "蓝牙",
            "蓝牙": "蓝牙",
            "无线蓝牙": "蓝牙",
            "2.4g": "2.4G 无线",
            "2.4g 无线": "2.4G 无线",
            "2.4g wireless": "2.4G 无线",
            "2.4ghz": "2.4G 无线",
            "2.4ghz 无线": "2.4G 无线",
            "2.4ghz wireless": "2.4G 无线",
            "三模": "三模",
            "tri-mode": "三模",
            "tri mode": "三模",
        }
        if lowered in exact_connection_aliases:
            return exact_connection_aliases[lowered]
        if lowered in {
            "true",
            "yes",
            "1",
            "wireless",
            "wifi",
            "无线",
        }:
            return "Wireless"
        if lowered in {
            "false",
            "no",
            "0",
            "wired",
            "cable",
            "有线",
            "有线连接",
        }:
            return "Wired"
        if lowered == "usb":
            return "USB"
    if key == "wireless":
        if lowered in {"true", "yes", "1", "wireless", "是", "无线"}:
            return "true"
        if lowered in {"false", "no", "0", "wired", "否", "有线"}:
            return "false"
    if key == "color":
        color_aliases = {
            "black": "Black",
            "white": "White",
            "silver": "Silver",
            "gray": "Gray",
            "grey": "Gray",
            "pink": "Pink",
            "黑": "黑色",
            "黑色": "黑色",
            "白": "白色",
            "白色": "白色",
            "银": "银色",
            "银色": "银色",
            "灰": "灰色",
            "灰色": "灰色",
            "粉": "粉色",
            "粉色": "粉色",
        }
        return color_aliases.get(lowered, stripped)
    if key == "switches":
        switch_aliases = {
            "red": "Red",
            "blue": "Blue",
            "brown": "Brown",
            "magnetic": "Magnetic",
            "红": "红轴",
            "红轴": "红轴",
            "青": "青轴",
            "青轴": "青轴",
            "茶": "茶轴",
            "茶轴": "茶轴",
            "磁": "磁轴",
            "磁轴": "磁轴",
        }
        return switch_aliases.get(lowered, stripped)
    if key == "refresh_rate":
        if match := re.search(r"(\d{2,3})\s*(?:hz|赫兹)?", lowered):
            return f"{match.group(1)}Hz"
    if key == "frame_rate":
        if match := re.search(r"(\d{2,3})\s*(?:fps)?", lowered):
            return f"{match.group(1)}fps"
    if key == "power_w":
        if match := re.search(r"(\d{1,4})\s*(?:w|瓦)?", lowered):
            return match.group(1)
    if key == "resolution":
        resolution_aliases = {
            "2k": "2560x1440",
            "1440p": "2560x1440",
            "4k": "4K",
            "1080p": "1080p",
        }
        return resolution_aliases.get(lowered.replace(" ", ""), stripped)
    if key in {"microphone", "backlit"}:
        if lowered in {"true", "yes", "1", "有", "带", "是"}:
            return "Yes"
        if lowered in {"false", "no", "0", "无", "不带", "否"}:
            return "No"
    return stripped


def _canonical_category(category: str | None) -> str | None:
    if not category:
        return None
    lowered = category.strip().lower()
    category_aliases = {
        "mouse": {
            "mouse",
            "mice",
            "鼠标",
            "游戏鼠标",
            str(CATEGORY_ALIASES.get("mouse", "")).lower(),
        },
        "keyboard": {
            "keyboard",
            "keyboards",
            "键盘",
            "机械键盘",
            str(CATEGORY_ALIASES.get("keyboard", "")).lower(),
        },
        "headset": {
            "headset",
            "headsets",
            "headphone",
            "headphones",
            "earphone",
            "earphones",
            "耳机",
            "耳麦",
            "头戴耳机",
            "游戏耳机",
            str(CATEGORY_ALIASES.get("headset", "")).lower(),
        },
        "monitor": {
            "monitor",
            "monitors",
            "display",
            "screen",
            "显示器",
            "屏幕",
            str(CATEGORY_ALIASES.get("monitor", "")).lower(),
        },
        "speaker": {
            "speaker",
            "speakers",
            "音箱",
            "音响",
            "蓝牙音箱",
            str(CATEGORY_ALIASES.get("speaker", "")).lower(),
        },
        "webcam": {
            "webcam",
            "webcams",
            "camera",
            "摄像头",
            "网络摄像头",
            str(CATEGORY_ALIASES.get("webcam", "")).lower(),
        },
    }
    for canonical, aliases in category_aliases.items():
        if lowered in {alias for alias in aliases if alias}:
            return canonical
    return lowered
