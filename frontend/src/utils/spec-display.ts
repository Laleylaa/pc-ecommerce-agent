import { specCategoryAliases, specDisplayRegistry } from "../generated/spec-display";

function lookup<T>(values: Record<string, T>, key: string): T | undefined {
  return Object.hasOwn(values, key) ? values[key] : undefined;
}

/** Display only: retain source keys, values and SKU/SPU ownership in application state. */
export function formatSpec(key: string, value: unknown, category?: string) {
  const definition = lookup(specDisplayRegistry, key.trim().toLowerCase());
  const categoryName = lookup(specCategoryAliases, (category ?? "").toLowerCase()) ?? category ?? "";
  const label = definition
    ? lookup(definition.categoryLabels, categoryName) ?? definition.label : key;
  const formatValue = (item: unknown): string => {
    if (item === null || item === undefined || item === "") return "未标注";
    if (Array.isArray(item)) return item.length ? item.map(formatValue).join(" / ") : "未标注";
    if (typeof item === "object") return JSON.stringify(item);
    const raw = String(item);
    if (!definition) return raw;
    const text = raw.trim();
    if (!text) return "未标注";
    if (definition.unit && /^[+-]?\d+(?:\.\d+)?$/.test(text)) return `${text} ${definition.unit}`;
    // No unit conversion, substring replacement or inference from query matching groups.
    return lookup(definition.values, text.toLowerCase()) ?? raw;
  };
  return { key, label, value: formatValue(value), mapped: !!definition };
}

export function formatSpecs(specs: Record<string, unknown> | null | undefined, category?: string) {
  return Object.entries(specs ?? {}).map(([key, value]) => formatSpec(key, value, category));
}
