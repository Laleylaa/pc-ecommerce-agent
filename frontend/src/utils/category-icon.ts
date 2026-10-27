import { Headphones, Keyboard, Mouse, Package, Monitor, Speaker, Webcam } from "lucide-react";

/**
 * Shared category-to-icon mapping for product cards.
 * Used by product summary cards and the detail drawer.
 */
export const CATEGORY_ICON_MAP: Record<string, typeof Mouse> = {
  mouse: Mouse,
  keyboard: Keyboard,
  headphone: Headphones,
  headset: Headphones,
  monitor: Monitor,
  speaker: Speaker,
  webcam: Webcam,
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
  if (lower.includes("显示器")) return Monitor;
  if (lower.includes("音箱")) return Speaker;
  if (lower.includes("摄像头")) return Webcam;
  return DEFAULT_CATEGORY_ICON;
}
