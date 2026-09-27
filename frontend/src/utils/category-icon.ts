import { Headphones, Keyboard, Mouse, Package } from "lucide-react";

/**
 * Shared category-to-icon mapping for product cards.
 * Used by both ProductInlineCard and ContextPanel.
 */
export const CATEGORY_ICON_MAP: Record<string, typeof Mouse> = {
  mouse: Mouse,
  keyboard: Keyboard,
  headphone: Headphones,
  headset: Headphones,
};

export const DEFAULT_CATEGORY_ICON = Package;

/**
 * Returns the appropriate Lucide icon component for a product category.
 * Matches Chinese and English terms.
 */
export function getCategoryIcon(category: string): typeof Mouse {
  const lower = category.toLowerCase();
  for (const [key, Icon] of Object.entries(CATEGORY_ICON_MAP)) {
    if (lower.includes(key)) return Icon;
  }
  // Also match Chinese terms
  if (lower.includes("鼠标")) return Mouse;
  if (lower.includes("键盘")) return Keyboard;
  if (lower.includes("耳机")) return Headphones;
  return DEFAULT_CATEGORY_ICON;
}

/**
 * CSS class names for product category thumbnails, matching the PCB design system.
 */
export const CATEGORY_THUMB_CLASS: Record<string, string> = {
  mouse: "product-inline-thumb--mouse",
  keyboard: "product-inline-thumb--keyboard",
  headphone: "product-inline-thumb--headphone",
  headset: "product-inline-thumb--headphone",
};

/**
 * Returns the CSS class name for a product category's thumb gradient.
 */
export function getCategoryThumbClass(category: string): string {
  const lower = category.toLowerCase();
  for (const [key, cls] of Object.entries(CATEGORY_THUMB_CLASS)) {
    if (lower.includes(key)) return cls;
  }
  if (lower.includes("鼠标")) return CATEGORY_THUMB_CLASS.mouse;
  if (lower.includes("键盘")) return CATEGORY_THUMB_CLASS.keyboard;
  if (lower.includes("耳机")) return CATEGORY_THUMB_CLASS.headphone;
  return "product-inline-thumb--default";
}