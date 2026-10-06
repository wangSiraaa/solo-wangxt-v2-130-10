import { useEffect, useRef } from 'react';
import cytoscape, { Core, CoreLayoutOptions, ElementDefinition } from 'cytoscape';

interface Props {
  elements: ElementDefinition[];
}

export function NetworkGraph({ elements }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const cy = cytoscape({
      container: containerRef.current,
      elements,
      style: [
        {
          selector: 'node',
          style: {
            label: 'data(label)',
            'background-color': '#2563eb',
            color: '#fff',
            'font-size': 10,
            'text-valign': 'bottom',
            'text-margin-y': 6,
            width: 18,
            height: 18
          }
        },
        {
          selector: 'edge',
          style: {
            label: 'data(label)',
            width: 1.2,
            'line-color': '#64748b',
            'target-arrow-color': '#64748b',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
            'font-size': 8
          }
        }
      ],
      layout: { name: 'cose', animate: false, nodeRepulsion: 8000 } as CoreLayoutOptions
    });
    cyRef.current = cy;
    return () => cy.destroy();
  }, [elements]);

  return <div ref={containerRef} style={{ height: 520, border: '1px solid #cbd5e1', borderRadius: 8 }} />;
}
