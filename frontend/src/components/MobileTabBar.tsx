import { BookOpenText, MessageSquare, PackageSearch } from "lucide-react";

export type MobileTab = "chat" | "products" | "details";

type MobileTabBarProps = {
  activeTab: MobileTab;
  onTabChange: (tab: MobileTab) => void;
  productCount: number;
  evidenceCount: number;
};

export function MobileTabBar({ activeTab, onTabChange, productCount, evidenceCount }: MobileTabBarProps) {
  return (
    <nav className="mobile-tab-bar">
      <button
        type="button"
        className={`mobile-tab ${activeTab === "chat" ? "active" : ""}`}
        onClick={() => onTabChange("chat")}
      >
        <MessageSquare size={18} />
        <span>对话</span>
      </button>
      <button
        type="button"
        className={`mobile-tab ${activeTab === "products" ? "active" : ""}`}
        onClick={() => onTabChange("products")}
      >
        <PackageSearch size={18} />
        <span>商品{productCount > 0 ? ` (${productCount})` : ""}</span>
      </button>
      <button
        type="button"
        className={`mobile-tab ${activeTab === "details" ? "active" : ""}`}
        onClick={() => onTabChange("details")}
      >
        <BookOpenText size={18} />
        <span>详情{evidenceCount > 0 ? ` (${evidenceCount})` : ""}</span>
      </button>
    </nav>
  );
}