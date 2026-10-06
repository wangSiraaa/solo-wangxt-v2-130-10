declare module 'cytoscape' {
  export interface ElementDefinition {
    data: Record<string, unknown>;
  }

  export interface CoreLayoutOptions {
    name: string;
    [key: string]: unknown;
  }

  export interface Core {
    destroy(): void;
  }

  interface CytoscapeOptions {
    container: HTMLElement;
    elements: ElementDefinition[];
    style: unknown[];
    layout: CoreLayoutOptions;
  }

  export default function cytoscape(options: CytoscapeOptions): Core;
}
