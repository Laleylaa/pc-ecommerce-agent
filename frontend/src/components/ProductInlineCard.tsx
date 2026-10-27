import { useState } from "react";
import { ArrowUpRight, ArrowRight } from "lucide-react";
import type { ProductCard } from "../types";
import { getCategoryIcon } from "../utils/category-icon";
import { formatSpecs } from "../utils/spec-display";

export function productPresentation(product: ProductCard) {
  const series = product.entity_scope === "spu" || product.ranking_scope === "spu";
  const price = series && product.series_min_price != null
    ? product.series_max_price && product.series_max_price !== product.series_min_price
      ? `¥${product.series_min_price}–${product.series_max_price}` : `¥${product.series_min_price}`
    : `¥${product.price}`;
  return {
    series,
    title: series ? product.spu_title || product.title : product.title,
    price,
    specs: series ? product.series_common_specs ?? {} : product.specs,
    priceScope: series ? product.match_kind ? "本次候选版本价格" : "型号价格范围" : "当前版本价格"
  };
}

export function ProductImage({ product }: { product: ProductCard }) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const Icon = getCategoryIcon(product.category);
  return product.image_url && failedUrl !== product.image_url
    ? <img src={product.image_url} alt={productPresentation(product).title} loading="lazy"
        onError={() => setFailedUrl(product.image_url ?? null)} />
    : <div className="product-placeholder"><Icon size={52} strokeWidth={1.15} /><span>{product.category} · 暂无商品图片</span></div>;
}

export function ProductInlineCard({ product, onClick }: { product: ProductCard; onClick?: () => void }) {
  const view = productPresentation(product);
  return (
    <article className="product-inline-card">
      <div className="product-inline-thumb"><ProductImage product={product} /></div>
      <div className="product-inline-info">
        <span className="eyebrow">{product.brand} · {product.category}</span>
        <h4 title={view.title}>{view.title}</h4>
        {product.match_kind === "alternative" && <span className="difference-label">相近候选 · 条件有差异</span>}
        {product.match_kind === "object_facts" && <small>商品资料 · 未确认满足全部要求</small>}
        <div className="spec-chips">{formatSpecs(view.specs, product.category).slice(0, 2).map(({ key, label, value }) =>
          <span key={key}>{label}：{value}</span>)}</div>
      </div>
      <div className="product-inline-price"><div><small>{view.priceScope}</small><strong>{view.price}</strong></div>
        <button type="button" className="text-button" onClick={onClick} disabled={!onClick}>
          {view.series ? "查看版本" : "查看详情"}<ArrowUpRight size={15} />
        </button>
      </div>
    </article>
  );
}

export function ProductCardRow({ products, onProductClick, onViewAll }: {
  products: ProductCard[]; onProductClick?: (product: ProductCard) => void;
  onViewAll?: (products: ProductCard[]) => void;
}) {
  if (!products.length) return null;
  const visible = products.slice(0, 2);
  return <section className="product-card-row" aria-label="本次查询商品">
    <div className="result-heading"><strong>本次返回 {products.length} 个候选</strong>
      {products.length > 2 && <button type="button" className="text-button" onClick={() => onViewAll?.(products)} disabled={!onViewAll}>
        查看全部 {products.length} 款<ArrowRight size={14} /></button>}</div>
    <div className="product-inline-list">{visible.map((product, index) =>
      <ProductInlineCard key={`${product.spu_id}-${product.sku_id}-${index}`} product={product}
        onClick={onProductClick ? () => onProductClick(product) : undefined} />)}</div>
  </section>;
}
