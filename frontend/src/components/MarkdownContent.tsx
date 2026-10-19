import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";

type MarkdownContentProps = {
  children: string;
};

const components: Components = {
  table: ({ children }) => (
    <div className="markdown-table" role="region" aria-label="回答中的表格" tabIndex={0}>
      <table>{children}</table>
    </div>
  ),
  // Open links in new tab for security
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
};

export function MarkdownContent({ children }: MarkdownContentProps) {
  if (!children) return null;

  return (
    <div className="markdown-content">
      <ReactMarkdown
        remarkPlugins={[[remarkGfm, { singleTilde: false }]]}
        components={components}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
