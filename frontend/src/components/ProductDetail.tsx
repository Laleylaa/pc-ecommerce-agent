import { useState } from "react";
import type { ProductCard } from "../types";
import { ProductImage, productPresentation } from "./ProductInlineCard";
import { formatSpecs } from "../utils/spec-display";

export function ProductDetail({ product, onAsk }: { product: ProductCard; onAsk: (text: string) => void }) {
  const view = productPresentation(product);
  const [skuId, setSkuId] = useState<number | null>(null);
  const variant = product.series_variants?.find(item => item.sku_id === skuId);
  const specs = variant?.specs ?? view.specs;
  return <div className="product-detail-content">
    <div className="detail-image"><ProductImage product={variant ? { ...product, ...variant } : product} /></div>
    <span className="eyebrow">{product.brand} · {product.category}</span>
    <h2>{view.title}</h2>
    {product.match_kind === "alternative" && <p className="difference-label">相近候选，存在条件差异，请结合回答中的说明选择。</p>}
    {product.match_kind === "object_facts" && <p className="muted">对象资料，未确认满足全部要求。</p>}
    <div className="detail-price">{variant ? `¥${variant.price}` : view.price}</div>
    <small>{variant ? "所选版本价格" : view.priceScope} · 查询时信息</small>
    {view.series && !!product.series_variants?.length && <label className="variant-select">查看具体版本
      <select value={skuId ?? ""} onChange={event => setSkuId(event.target.value ? Number(event.target.value) : null)}>
        <option value="">型号概览</option>
        {product.series_variants.map(item => <option key={item.sku_id} value={item.sku_id}>{item.title} · ¥{item.price}</option>)}
      </select><small>仅列出本次返回的版本资料。</small>
    </label>}
    <h3>{view.series && !variant ? "各版本共有规格" : "版本规格"}</h3>
    <dl className="spec-table">{formatSpecs(specs, product.category).map(({ key, label, value }) => <div key={key}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    {!Object.keys(specs).length && <p className="muted">本次没有返回可展示的规格。</p>}
    {view.series && !variant && !!Object.keys(product.series_option_specs ?? {}).length && <>
      <h3>版本可选规格</h3><dl className="spec-table">{formatSpecs(product.series_option_specs, product.category).map(({ key, label, value }) =>
        <div key={key}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
      <p className="muted">不同选项不一定能组合在同一版本中。</p>
    </>}
    {(variant || !view.series) && <p>查询时库存：{variant?.stock ?? product.stock}</p>}
    <button className="primary-button" type="button" onClick={() => onAsk(variant
      ? `请介绍 SKU ${variant.sku_id}（${variant.title}）的详细配置。`
      : `请介绍${view.series ? ` SPU ${product.spu_id}` : ` SKU ${product.sku_id}`}（${view.title}）的详细配置。`)}>继续询问这款商品</button>
    <p className="muted">价格与库存可能变化，可继续咨询以重新查询。</p>
  </div>;
}
