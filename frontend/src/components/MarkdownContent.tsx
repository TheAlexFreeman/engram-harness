import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";

interface Props {
  content: string;
}

/**
 * Renders agent response text as formatted markdown.
 *
 * Features:
 * - GitHub-Flavored Markdown (tables, strikethrough, task lists, autolinks)
 * - Syntax-highlighted fenced code blocks (via highlight.js github-dark theme)
 * - Inline code pill styling
 * - Dark-UI-matched prose via .agent-prose (globals.css)
 */
export function MarkdownContent({ content }: Props) {
  return (
    <div className="agent-prose">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          // Open links in new tab (agent responses may include URLs)
          a: ({ href, children, ...props }) => (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              {...props}
            >
              {children}
            </a>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
