"""流程事项域目录的加载与选择。"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class ProcessDomain:
    """一个可用于流程分流的业务事项域。"""

    code: str
    name: str
    enabled: bool
    keywords: tuple[str, ...]
    process_signals: tuple[str, ...]
    exclude_signals: tuple[str, ...]


@dataclass(frozen=True)
class ProcessDomainCatalog:
    """可审核、可版本化的事项域目录。"""

    version: str
    domains: tuple[ProcessDomain, ...]

    @property
    def enabled_domains(self) -> tuple[ProcessDomain, ...]:
        """返回按配置顺序启用的事项域。"""
        return tuple(domain for domain in self.domains if domain.enabled)

    def select_domains(self, codes: Sequence[str] | None = None) -> tuple[ProcessDomain, ...]:
        """按调用方指定的范围选择事项域；未指定时使用全部启用项。"""
        if codes is None:
            return self.enabled_domains

        selected_codes = tuple(str(code).strip() for code in codes if str(code).strip())
        if not selected_codes:
            return self.enabled_domains
        if len(selected_codes) != len(set(selected_codes)):
            raise ValueError("事项域代码不能重复。")

        by_code = {domain.code: domain for domain in self.domains}
        selected: list[ProcessDomain] = []
        for code in selected_codes:
            domain = by_code.get(code)
            if domain is None:
                raise ValueError(f"事项域代码不存在：{code}")
            if not domain.enabled:
                raise ValueError(f"事项域未启用：{code}")
            selected.append(domain)
        return tuple(selected)


def _read_text_items(
    payload: object,
    field_name: str,
    domain_code: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    """验证配置中的关键词数组，拒绝空值以避免静默失效。"""
    if not isinstance(payload, list) or (not payload and not allow_empty):
        raise ValueError(f"事项域 {domain_code} 的 {field_name} 必须是非空数组。")
    values = tuple(str(item).strip() for item in payload if str(item).strip())
    if len(values) != len(payload):
        raise ValueError(f"事项域 {domain_code} 的 {field_name} 不能包含空值。")
    return values


def load_process_domain_catalog(path: str | Path | None = None) -> ProcessDomainCatalog:
    """读取事项域 JSON 配置，并在启动时完成基本结构校验。"""
    catalog_path = Path(path) if path is not None else Path(__file__).with_name("domain_catalog.json")
    try:
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"未找到事项域目录：{catalog_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"事项域目录不是有效 JSON：{catalog_path}") from exc

    if not isinstance(payload, dict):
        raise ValueError("事项域目录必须是 JSON 对象。")
    version = str(payload.get("version") or "").strip()
    raw_domains = payload.get("domains")
    if not version:
        raise ValueError("事项域目录缺少版本号。")
    if not isinstance(raw_domains, list) or not raw_domains:
        raise ValueError("事项域目录至少需要一个事项域。")

    domains: list[ProcessDomain] = []
    codes: set[str] = set()
    for item in raw_domains:
        if not isinstance(item, dict):
            raise ValueError("事项域配置项必须是 JSON 对象。")
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        if not code or not name:
            raise ValueError("事项域配置项缺少 code 或 name。")
        if code in codes:
            raise ValueError(f"事项域代码重复：{code}")
        if not isinstance(item.get("enabled"), bool):
            raise ValueError(f"事项域 {code} 的 enabled 必须是布尔值。")
        domains.append(ProcessDomain(
            code=code,
            name=name,
            enabled=item["enabled"],
            keywords=_read_text_items(item.get("keywords"), "keywords", code),
            process_signals=_read_text_items(item.get("process_signals"), "process_signals", code),
            exclude_signals=_read_text_items(
                item.get("exclude_signals"),
                "exclude_signals",
                code,
                allow_empty=True,
            ),
        ))
        codes.add(code)
    return ProcessDomainCatalog(version=version, domains=tuple(domains))
