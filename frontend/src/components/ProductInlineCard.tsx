import { ProductCard } from "../types";
import { getCategoryIcon, getCategoryThumbClass } from "../utils/category-icon";

type ProductInlineCardProps = {
  product: ProductCard;
  onClick?: () => void;
};

export function ProductInlineCard({ product, onClick }: ProductInlineCardProps) {
  const IconComponent = getCategoryIcon(product.category);
  const thumbClass = getCategoryThumbClass(product.category);
  const isSeriesProduct = product.entity_scope === "spu" || product.ranking_scope === "spu";
  const displayTitle = isSeriesProduct && product.spu_title ? product.spu_title : product.title;
  const specLine = isSeriesProduct
    ? product.series_sku_count
      ? `共 ${product.series_sku_count} 个在售版本`
      : "商品系列"
    : Object.entries(product.specs)
        .slice(0, 3)
        .map(([key, value]) => `${key}: ${value}`)
        .join(" · ");
  const priceLabel =
    isSeriesProduct && product.series_min_price
      ? product.series_max_price &&
        product.series_max_price !== product.series_min_price
        ? `¥${product.series_min_price}–${product.series_max_price}`
        : `¥${product.series_min_price}`
      : `¥${product.price}`;
  const stockLabel =
    isSeriesProduct && product.series_total_stock !== null &&
    product.series_total_stock !== undefined
      ? `系列库存 ${product.series_total_stock}`
      : product.stock > 0
        ? `库存 ${product.stock}`
        : "缺货";

  function handleKeyDown(event: React.KeyboardEvent) {
    if (onClick && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      onClick();
    }
  }

  return (
    <article
      className="product-inline-card"
      onClick={onClick}
      onKeyDown={onClick ? handleKeyDown : undefined}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
    >
      <div className={`product-inline-thumb ${thumbClass}`}>
        {product.image_url ? (
          <img src={product.image_url} alt={displayTitle} loading="lazy" />
        ) : (
          <IconComponent size={24} />
        )}
      </div>
      <div className="product-inline-info">
        <h4>{displayTitle}</h4>
        <small>{product.brand} · {product.category}</small>
        {specLine && <small className="specs">{specLine}</small>}
      </div>
      <div className="product-inline-price">
        <strong>{priceLabel}</strong>
        <span>{stockLabel}</span>
      </div>
    </article>
  );
}

type ProductCardRowProps = {
  products: ProductCard[];
  onProductClick?: (product: ProductCard) => void;
};

const MAX_INLINE_PRODUCTS = 3;

export function ProductCardRow({ products, onProductClick }: ProductCardRowProps) {
  if (products.length === 0) return null;

  const visibleProducts = products.slice(0, MAX_INLINE_PRODUCTS);
  const remainingCount = products.length - visibleProducts.length;
  const candidateUnit = products.every(
    (product) => product.entity_scope === "spu" || product.ranking_scope === "spu",
  )
    ? "个系列"
    : "个候选";

  return (
    <div className="product-card-row" aria-label={`推荐商品摘要，共 ${products.length} 个商品`}>
      <div className="product-card-row-head">
        <strong>推荐商品</strong>
        <span>{products.length} {candidateUnit}</span>
      </div>
      <div className="product-inline-list">
        {visibleProducts.map((product) => (
          <ProductInlineCard
            key={product.sku_id}
            product={product}
            onClick={onProductClick ? () => onProductClick(product) : undefined}
          />
        ))}
      </div>
      {remainingCount > 0 && (
        <div className="product-inline-more">另有 {remainingCount} 个候选</div>
      )}
    </div>
  );
}
