import { useLayoutEffect, useRef, useState } from "react";
import { ArrowLeft } from "lucide-react";
import type { ProductCard } from "../types";
import { DetailDrawer } from "./DetailDrawer";
import { ProductDetail } from "./ProductDetail";
import { ProductInlineCard } from "./ProductInlineCard";

export function ProductResultsDrawer({ products, initialProduct, onClose, onAsk }: {
  products: ProductCard[]; initialProduct?: ProductCard | null;
  onClose: () => void; onAsk: (text: string) => void;
}) {
  const [selected, setSelected] = useState(initialProduct ?? null);
  const contentRef = useRef<HTMLDivElement>(null);
  const listPosition = useRef(0);
  const lastIndex = useRef<number | null>(null);
  useLayoutEffect(() => {
    const body = contentRef.current?.closest(".drawer-body");
    if (body) body.scrollTop = selected ? 0 : listPosition.current;
    if (lastIndex.current !== null) {
      const target = selected ? contentRef.current?.querySelector<HTMLButtonElement>(".product-back")
        : contentRef.current?.querySelectorAll<HTMLButtonElement>(".product-inline-card button")[lastIndex.current];
      target?.focus({ preventScroll: true });
    }
  }, [selected]);
  return <DetailDrawer title={selected ? "商品详情" : `本次商品 · ${products.length} 款`} onClose={onClose}>
    <div ref={contentRef}>
      {selected ? <>
        {!initialProduct && <button className="text-button product-back" type="button" onClick={() => setSelected(null)}>
          <ArrowLeft size={15} />返回全部 {products.length} 款</button>}
        <ProductDetail key={`${selected.spu_id}-${selected.sku_id}`} product={selected} onAsk={onAsk} />
      </> : <>
        <p className="result-caption">本次回复的全部候选，按返回顺序展示。点击查看具体版本与参数。</p>
        <div className="product-drawer-list">{products.map((product, index) =>
          <ProductInlineCard key={`${product.spu_id}-${product.sku_id}-${index}`} product={product} onClick={() => {
            listPosition.current = contentRef.current?.closest(".drawer-body")?.scrollTop ?? 0;
            lastIndex.current = index;
            setSelected(product);
          }} />)}</div>
      </>}
    </div>
  </DetailDrawer>;
}
