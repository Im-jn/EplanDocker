import './style.css'
import { GlobalWorkerOptions, getDocument } from 'pdfjs-dist/legacy/build/pdf.mjs'
import workerUrl from 'pdfjs-dist/legacy/build/pdf.worker.mjs?url'
import { commandsToPolylines, parseSnippetToPathCommands } from './shapeSearch'

function installPdfJsCollectionPolyfills(): void {
  const mapPrototype = Map.prototype as Map<unknown, unknown> & {
    getOrInsertComputed?: (key: unknown, callbackfn: (key: unknown) => unknown) => unknown
    getOrInsert?: (key: unknown, value: unknown) => unknown
  }
  if (!mapPrototype.getOrInsertComputed) {
    mapPrototype.getOrInsertComputed = function (key, callbackfn) {
      if (!this.has(key)) {
        this.set(key, callbackfn(key))
      }
      return this.get(key)
    }
  }
  if (!mapPrototype.getOrInsert) {
    mapPrototype.getOrInsert = function (key, value) {
      if (!this.has(key)) {
        this.set(key, value)
      }
      return this.get(key)
    }
  }

  const weakMapPrototype = WeakMap.prototype as WeakMap<object, unknown> & {
    getOrInsertComputed?: (key: object, callbackfn: (key: object) => unknown) => unknown
    getOrInsert?: (key: object, value: unknown) => unknown
  }
  if (!weakMapPrototype.getOrInsertComputed) {
    weakMapPrototype.getOrInsertComputed = function (key, callbackfn) {
      if (!this.has(key)) {
        this.set(key, callbackfn(key))
      }
      return this.get(key)
    }
  }
  if (!weakMapPrototype.getOrInsert) {
    weakMapPrototype.getOrInsert = function (key, value) {
      if (!this.has(key)) {
        this.set(key, value)
      }
      return this.get(key)
    }
  }
}

installPdfJsCollectionPolyfills()

GlobalWorkerOptions.workerSrc = workerUrl

type BBox = {
  x0: number
  y0: number
  x1: number
  y1: number
  width: number
  height: number
}

type ReaderSource = {
  object_ref: string | null
  context_chain: string[]
  snippet: string
  highlight_start: number
  highlight_end: number
}

type RelatedObjectReference = {
  object_ref: string
  role: string
}

type ObjectDetail = {
  object_ref: string
  kind_label: string
  description: string
  raw_source: string | null
  decoded_stream_preview: string | null
}

type VectorItem = {
  id: string
  kind: 'vector_path'
  page_number: number
  paint_operator: string
  bbox: BBox
  commands: Array<Record<string, unknown>>
  line_width: number
  effective_line_width: number
  source: ReaderSource
  source_comment: string
  reference_chain: RelatedObjectReference[]
  summary: {
    command_count: number
    point_count: number
  }
}

type ComponentItem = {
  id: string
  kind: 'component'
  page_number: number
  bbox: BBox
  component: {
    label?: string
    nearby_text?: string[]
    detection_source?: string
    link_kind?: string | null
    target?: unknown
    annotation_refs?: string[]
    action_refs?: string[]
    duplicate_count?: number
  }
  source: ReaderSource
  source_comment: string
  reference_chain: RelatedObjectReference[]
}

type LinkItem = {
  id: string
  kind: 'link'
  page_number: number
  bbox: BBox
  link: {
    kind: string | null
    target: {
      page?: number | string
      target_highlight_region?: {
        type?: string
        x?: number
        y?: number
        width?: number
        height?: number
        x0?: number
        y0?: number
        x1?: number
        y1?: number
        bbox?: Partial<BBox>
        coordinate_space?: string
        source?: string
      } | null
    } | null
    action: unknown
  }
  source: ReaderSource
  source_comment: string
  reference_chain: RelatedObjectReference[]
}

type TextItem = {
  id: string
  kind: 'text'
  page_number: number
  bbox: BBox
  text: {
    content: string
    raw_glyph_text: string | null
    operator: string
    font: string | null
    font_size: number
    decoded_via_tounicode: boolean
  }
  source: ReaderSource
  source_comment: string
  reference_chain: RelatedObjectReference[]
  summary: {
    char_count: number
  }
}

type ImageItem = {
  id: string
  kind: 'image'
  page_number: number
  bbox: BBox
  image: {
    name: string
    pixel_width: number | null
    pixel_height: number | null
    filters: string[]
    object_ref: string
  }
  source: ReaderSource
  source_comment: string
  reference_chain: RelatedObjectReference[]
  summary: {
    draw_width: number
    draw_height: number
  }
}

type ReaderItem = VectorItem | LinkItem | TextItem | ImageItem

type OverlayKind = ReaderItem['kind']

const LAYER_STORAGE_KEY = 'pdf-reader-overlay-layers'
const LAYOUT_STORAGE_KEY = 'pdf-reader-layout-sizes'

type LayoutSizes = {
  sidebarWidth: number
  inspectorWidth: number
  viewerHeight: number
  playgroundEditorWidth: number
}

const DEFAULT_LAYOUT_SIZES: LayoutSizes = {
  sidebarWidth: 320,
  inspectorWidth: 420,
  viewerHeight: 860,
  playgroundEditorWidth: 360,
}

function loadLayerVisibility(): Record<OverlayKind, boolean> {
  const defaults: Record<OverlayKind, boolean> = {
    vector_path: true,
    text: true,
    image: true,
    link: true,
  }
  try {
    const raw = localStorage.getItem(LAYER_STORAGE_KEY)
    if (!raw) {
      return defaults
    }
    const parsed = JSON.parse(raw) as Partial<Record<OverlayKind, boolean>>
    return { ...defaults, ...parsed }
  } catch {
    return defaults
  }
}

function saveLayerVisibility(visibility: Record<OverlayKind, boolean>): void {
  try {
    localStorage.setItem(LAYER_STORAGE_KEY, JSON.stringify(visibility))
  } catch {
    /* ignore quota / private mode */
  }
}

let layerVisibility = loadLayerVisibility()

function loadLayoutSizes(): LayoutSizes {
  try {
    const raw = localStorage.getItem(LAYOUT_STORAGE_KEY)
    if (!raw) {
      return DEFAULT_LAYOUT_SIZES
    }
    const parsed = JSON.parse(raw) as Partial<LayoutSizes>
    return {
      sidebarWidth: clamp(Number(parsed.sidebarWidth) || DEFAULT_LAYOUT_SIZES.sidebarWidth, 240, 520),
      inspectorWidth: clamp(Number(parsed.inspectorWidth) || DEFAULT_LAYOUT_SIZES.inspectorWidth, 320, 720),
      viewerHeight: clamp(Number(parsed.viewerHeight) || DEFAULT_LAYOUT_SIZES.viewerHeight, 420, 1200),
      playgroundEditorWidth: clamp(
        Number(parsed.playgroundEditorWidth) || DEFAULT_LAYOUT_SIZES.playgroundEditorWidth,
        260,
        560,
      ),
    }
  } catch {
    return DEFAULT_LAYOUT_SIZES
  }
}

function saveLayoutSizes(sizes: LayoutSizes): void {
  try {
    localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(sizes))
  } catch {
    /* ignore quota / private mode */
  }
}

function isReaderItem(item: { kind: string }): item is ReaderItem {
  return (
    item.kind === 'vector_path' ||
    item.kind === 'link' ||
    item.kind === 'text' ||
    item.kind === 'image'
  )
}

function isComponentItem(item: { kind: string }): item is ComponentItem {
  return item.kind === 'component'
}

function isLayerVisible(kind: ReaderItem['kind']): boolean {
  return layerVisibility[kind]
}

type PageManifest = {
  page_number: number
  page_size: {
    width_pt: number
    height_pt: number
  }
  item_counts: {
    component?: number
    vector_path: number
    text: number
    image: number
    link: number
  }
  warnings: string[]
  data_url: string
}

type ReaderDocument = {
  id: string
  title: string
  pdf_url: string
  page_count: number
  pages: PageManifest[]
  resolved_object_count: number
  header: string
}

type Manifest = {
  documents: ReaderDocument[]
}

type PageData = {
  page_number: number
  page_object_ref: string | null
  page_size: {
    width_pt: number
    height_pt: number
  }
  content_streams: string[]
  item_counts: {
    component?: number
    vector_path: number
    text: number
    image: number
    link: number
  }
  warnings: string[]
  object_details: Record<string, ObjectDetail>
  items: ReaderItem[]
  components: ComponentItem[]
}

type PageState = {
  pageNumber: number
  pageData: PageData
  canvas: HTMLCanvasElement
  overlay: HTMLDivElement
  scale: number
}

type ZoomAnchor = {
  documentX: number
  documentY: number
  viewportX: number
  viewportY: number
}

type RenderPageOptions = {
  preserveSelection?: boolean
  anchor?: ZoomAnchor | null
}

type PlaygroundBounds = {
  minX: number
  minY: number
  maxX: number
  maxY: number
  width: number
  height: number
}

type PlaygroundData = {
  sourceBounds: PlaygroundBounds
  bounds: PlaygroundBounds
  polylines: Array<{
    points: [number, number][]
    closed: boolean
  }>
}

type PlaygroundView = {
  scale: number
  offsetX: number
  offsetY: number
}

type PlaygroundHover = {
  x: number
  y: number
  sourceX: number
  sourceY: number
  snapped: boolean
  label: string
}

type PlaygroundPoint = {
  x: number
  y: number
  sourceX: number
  sourceY: number
  label: string
  color: string
}

type PlaygroundComparison = {
  candidate: PlaygroundData
  matchedBaseIndexes: Set<number>
  matchedCandidateIndexes: Set<number>
}

function mustQuery<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector)
  if (!element) {
    throw new Error(`Missing required element: ${selector}`)
  }
  return element
}

const app = mustQuery<HTMLDivElement>('#app')

app.innerHTML = `
  <div class="layout">
    <aside class="sidebar">
      <div class="panel">
        <div class="panel-title-row">
          <div>
            <p class="eyebrow">PDF source reader</p>
            <h1 class="title">Eplan PDF Object Explorer</h1>
          </div>
        </div>
        <p class="muted">
          Choose any PDF from storage/data/eplan_pdf. The viewer renders the original PDF and overlays clickable vector and hyperlink regions.
        </p>
      </div>

      <div class="panel">
        <label class="field-label" for="doc-select">Document</label>
        <select id="doc-select" class="select"></select>
        <div id="doc-meta" class="meta-list"></div>
      </div>

      <div class="panel">
        <div class="page-toolbar">
          <button id="prev-page" class="button" type="button">Previous</button>
          <button id="next-page" class="button" type="button">Next</button>
        </div>
        <label class="field-label" for="page-select">Page</label>
        <select id="page-select" class="select"></select>
        <div id="page-meta" class="meta-list"></div>
      </div>

      <div class="panel">
        <p class="field-label">Overlay layers</p>
        <div class="legend">
          <label class="legend-item">
            <input type="checkbox" class="layer-toggle" data-layer-kind="vector_path" checked />
            <i class="legend-swatch vector" aria-hidden="true"></i>
            <span>Vector path</span>
          </label>
          <label class="legend-item">
            <input type="checkbox" class="layer-toggle" data-layer-kind="text" checked />
            <i class="legend-swatch text" aria-hidden="true"></i>
            <span>Text block</span>
          </label>
          <label class="legend-item">
            <input type="checkbox" class="layer-toggle" data-layer-kind="image" checked />
            <i class="legend-swatch image" aria-hidden="true"></i>
            <span>Image</span>
          </label>
          <label class="legend-item">
            <input type="checkbox" class="layer-toggle" data-layer-kind="link" checked />
            <i class="legend-swatch link" aria-hidden="true"></i>
            <span>Hyperlink</span>
          </label>
          <div class="legend-note">
            <span><i class="legend-swatch selected" aria-hidden="true"></i>Selected (when clicking a visible region)</span>
          </div>
        </div>
        <p class="muted small">
          Uncheck a layer to hide its highlights on the page. Click a visible region to inspect PDF source; right-click a hyperlink to follow it and highlight its target.
        </p>
      </div>

    </aside>
    <div class="resize-handle resize-handle-vertical" data-resize-target="sidebar" title="Resize sidebar"></div>

    <main class="workspace">
      <div class="workspace-main">
        <section class="viewer-shell">
          <div class="viewer-topbar">
            <div id="viewer-status" class="status">Loading manifest...</div>
            <div class="viewer-toolbar">
              <button
                id="area-select-toggle"
                class="button icon-button compact-icon-button"
                type="button"
                title="Select multiple components by dragging a rectangle"
                aria-label="Toggle area selection"
                aria-pressed="false"
              >
                <span class="icon-button-glyph selection-icon" aria-hidden="true">
                  <svg viewBox="0 0 20 20" focusable="false">
                    <path d="M3 7V3h4M13 3h4v4M17 13v4h-4M7 17H3v-4" />
                    <path d="M7 7h6v6H7z" />
                  </svg>
                </span>
              </button>
              <label class="zoom-control" for="zoom-input">
                <span>Zoom</span>
                <input id="zoom-input" class="zoom-input" type="number" min="50" max="400" step="5" value="100" />
                <span>%</span>
              </label>
              <span id="zoom-label" class="zoom-label">Fit width</span>
              <div class="detect-control-group" aria-label="Detection tools">
                <span class="detect-control-label">DETECT:</span>
                <button
                  id="split-page-detect"
                  class="button detect-button split-page-detect-button"
                  type="button"
                  title="Detect and draw the page drawing and information areas"
                  aria-pressed="false"
                >
                  split
                </button>
                <button
                  id="entity-detect"
                  class="button detect-button entity-detect-button"
                  type="button"
                  title="Detect bbox-connected vector entities on this page"
                  aria-pressed="false"
                >
                  entity
                </button>
                <button
                  id="box-detect"
                  class="button detect-button box-detect-button"
                  type="button"
                  title="Detect closed rectangular vector boxes inside the selected entity"
                  aria-pressed="false"
                  disabled
                >
                  box
                </button>
                <button
                  id="circle-detect"
                  class="button detect-button circle-detect-button"
                  type="button"
                  title="Detect closed circular vector regions inside the selected entity"
                  aria-pressed="false"
                  disabled
                >
                  circle
                </button>
                <button
                  id="dashed-detect"
                  class="button detect-button dashed-detect-button"
                  type="button"
                  title="Detect closed dashed vector polygons inside the selected entity"
                  aria-pressed="false"
                  disabled
                >
                  dashed
                </button>
                <button
                  id="cell-detect"
                  class="button detect-button cell-detect-button"
                  type="button"
                  title="Split vector intersections and detect every closed face inside the selected entity"
                  aria-pressed="false"
                  disabled
                >
                  cell
                </button>
                <button
                  id="pin-circle-detect"
                  class="button detect-button pin-circle-detect-button"
                  type="button"
                  title="Detect small closed circle ports inside the selected entity"
                  aria-pressed="false"
                  disabled
                >
                  pin circle
                </button>
                <button
                  id="pin-arrow-detect"
                  class="button detect-button pin-arrow-detect-button"
                  type="button"
                  title="Detect filled triangular arrow ports inside the selected entity"
                  aria-pressed="false"
                  disabled
                >
                  pin arrow
                </button>
                <button
                  id="wire-mark-detect"
                  class="button detect-button wire-mark-detect-button"
                  type="button"
                  title="Detect repeated short diagonal wire marks inside the selected entity"
                  aria-pressed="false"
                  disabled
                >
                  wire mark
                </button>
                <button
                  id="symbol-match-display"
                  class="button detect-button symbol-match-display-button is-active"
                  type="button"
                  title="Show or hide all symbol matches already found on this page"
                  aria-label="Show detected symbols"
                  aria-pressed="true"
                >
                  symbols
                </button>
              </div>
            </div>
          </div>
          <div id="viewer-scroll" class="viewer-scroll">
            <div id="viewer-stage" class="viewer-stage"></div>
          </div>
        </section>
        <div class="resize-handle resize-handle-horizontal" data-resize-target="viewer" title="Resize viewer"></div>

        <section class="playground-shell">
          <div class="playground-topbar">
            <div>
              <p class="eyebrow">PDF Play Ground</p>
              <h2 class="section-title">Vector Whiteboard</h2>
            </div>
            <div class="playground-toolbar">
              <div class="vector-search-group" aria-label="Shape search controls">
                <button
                  id="vector-match-search"
                  class="button"
                  type="button"
                  title="Find matching vector groups from the playground shape"
                  disabled
                >
                  Search
                </button>
                <label class="missing-vector-control" title="Allowed percentage of target vectors that may be missing">
                  <span>Missing</span>
                  <input id="missing-vector-input" type="number" min="0" max="99" step="1" value="10" />
                  <span>%</span>
                </label>
                <label class="scale-range-control" title="Allowed vector match scale range">
                  <span>Scale</span>
                  <input id="scale-min-input" type="number" min="0.01" step="0.05" value="0.5" />
                  <span>to</span>
                  <input id="scale-max-input" type="number" min="0.01" step="0.05" value="2.5" />
                </label>
                <select id="vector-match-results" class="results-select" aria-label="Search results" disabled>
                  <option value="">Results: None</option>
                </select>
              </div>
              <div class="playground-toolbar-divider" aria-hidden="true"></div>
              <button id="source-stash" class="button source-stash-button" type="button" disabled>Stash Shape</button>
              <span id="playground-stash-count" class="playground-stash-count">Stashed: 0 vectors</span>
              <button id="source-compare" class="button source-compare-button" type="button" hidden disabled>Compare</button>
              <label class="playground-shape-scale-control" title="Scale only the stashed shape preview">
                <span>Shape</span>
                <input id="playground-shape-scale" type="number" min="10" max="400" step="10" value="100" disabled />
                <span>%</span>
              </label>
              <div class="playground-toolbar-divider" aria-hidden="true"></div>
              <button id="playground-fit" class="button" type="button">Fit View</button>
              <button id="playground-reset" class="button" type="button">Clear</button>
            </div>
          </div>
          <div class="playground-content">
            <div class="playground-editor-pane">
              <label class="field-label" for="playground-code">PDF vector code block</label>
              <textarea
                id="playground-code"
                class="playground-code"
                spellcheck="false"
                placeholder="Paste PDF vector commands here, for example:&#10;19.724 841.89 m&#10;319.724 830.551 l&#10;S"
              ></textarea>
              <p id="playground-status" class="muted small">Paste vector path code to draw it on the whiteboard below.</p>
            </div>
            <div class="resize-handle resize-handle-vertical resize-handle-playground" data-resize-target="playground-editor" title="Resize editor"></div>
            <div id="playground-board" class="playground-board">
              <canvas id="playground-canvas" class="playground-canvas"></canvas>
              <div id="playground-empty" class="playground-empty">Vector preview will appear here.</div>
              <div id="playground-coords" class="playground-coords">x: -, y: -</div>
            </div>
          </div>
        </section>
      </div>
      <div class="resize-handle resize-handle-vertical" data-resize-target="inspector" title="Resize inspector"></div>

      <aside class="inspector">
        <div class="panel inspector-panel">
          <div class="inspector-tabs" role="tablist">
            <button
              id="tab-symbols"
              class="inspector-tab is-active"
              type="button"
              role="tab"
              aria-selected="true"
              data-tab="symbols"
            >
              Symbols
            </button>
            <button
              id="tab-extract-info"
              class="inspector-tab"
              type="button"
              role="tab"
              aria-selected="false"
              data-tab="extract-info"
            >
              Extract Info
            </button>
            <button
              id="tab-info-trace"
              class="inspector-tab"
              type="button"
              role="tab"
              aria-selected="false"
              data-tab="info-trace"
            >
              Info Trace
            </button>
            <button
              id="tab-source"
              class="inspector-tab"
              type="button"
              role="tab"
              aria-selected="false"
              data-tab="source"
            >
              PDF Source
            </button>
          </div>

          <div id="tab-panel-symbols" class="inspector-tab-panel" role="tabpanel">
            <p class="eyebrow">Symbol extraction</p>
            <h2 class="section-title">Symbol Overview</h2>
            <label class="field-label" for="symbol-pages-input">Symbol overview pages</label>
            <div class="symbol-page-controls">
              <input
                id="symbol-pages-input"
                class="symbol-pages-input"
                type="text"
                inputmode="numeric"
                spellcheck="false"
                placeholder="e.g. 5, 6"
              />
              <button id="symbol-add-page" class="button" type="button" title="Add the current page to the list">
                + current
              </button>
            </div>
            <button id="symbol-extract" class="button symbol-extract-button" type="button">
              Start extraction
            </button>
            <button
              id="symbol-search"
              class="button symbol-search-button"
              type="button"
              title="Match each extracted symbol inside the selected entity"
              disabled
            >
              Search in entity
            </button>
            <p id="symbol-status" class="muted small">Enter the symbol overview page numbers, then start extraction.</p>
            <div id="symbol-list" class="symbol-list"></div>
          </div>

          <div id="tab-panel-extract-info" class="inspector-tab-panel" role="tabpanel" hidden>
            <p class="eyebrow">Page extraction</p>
            <h2 class="section-title">Extract Info</h2>
            <p id="extract-symbol-dependency" class="extract-dependency muted small">
              Run extraction in the Symbols tab first.
            </p>
            <label class="extract-option">
              <input id="extract-llm-gate" type="checkbox" checked />
              <span>Use LLM electrical entity gate (off = dummy)</span>
            </label>
            <div class="extract-actions">
              <button id="extract-info-run" class="button extract-info-button" type="button" disabled>Extract</button>
              <button id="extract-info-cancel" class="button extract-cancel-button" type="button" disabled>Stop</button>
            </div>
            <div class="extract-progress" aria-live="polite">
              <div class="extract-progress-track"><div id="extract-progress-bar" class="extract-progress-bar"></div></div>
              <p id="extract-info-status" class="muted small">Extract the currently visible page.</p>
            </div>
            <div class="extract-json-search" aria-label="Search extracted JSON objects">
              <select id="extract-json-search-category" class="select extract-json-search-category" aria-label="Extracted object category">
                <option value="components">Component</option>
                <option value="elements">Element</option>
                <option value="wires">Wire</option>
                <option value="endpoints">Endpoint</option>
                <option value="nets">Net</option>
                <option value="groups">Group</option>
              </select>
              <input id="extract-json-search-id" class="extract-json-search-id" type="text" inputmode="numeric" placeholder="ID" aria-label="Extracted object ID" />
              <button id="extract-json-search-button" class="button" type="button">Search</button>
              <span id="extract-json-search-status" class="extract-json-search-status muted small"></span>
            </div>
            <div id="extract-info-result" class="extract-info-result"></div>
          </div>

          <div id="tab-panel-info-trace" class="inspector-tab-panel" role="tabpanel" hidden>
            <p class="eyebrow">Connectivity tracing</p>
            <h2 class="section-title">Info Trace</h2>
            <div class="info-trace-controls">
              <label class="field-label" for="info-trace-page">Page</label>
              <input id="info-trace-page" class="select" type="number" min="1" step="1" inputmode="numeric" />
              <label class="field-label" for="info-trace-kind">Target</label>
              <select id="info-trace-kind" class="select">
                <option value="component">Component</option>
                <option value="wire">Wire</option>
              </select>
              <label class="field-label" for="info-trace-id">ID</label>
              <select id="info-trace-id" class="select" disabled>
                <option value="">Select an ID</option>
              </select>
              <span></span>
              <div class="info-trace-choose-actions">
                <button id="info-trace-choose" class="button" type="button" disabled>Choose</button>
                <button id="info-trace-stop" class="button info-trace-danger" type="button" disabled>Stop Trace</button>
              </div>
            </div>
            <div class="info-trace-legend" aria-label="Trace highlight colors">
              <span><i class="info-trace-swatch info-trace-swatch-component"></i>Component</span>
              <span><i class="info-trace-swatch info-trace-swatch-wire"></i>Wire</span>
              <span><i class="info-trace-swatch info-trace-swatch-endpoint"></i>Endpoint</span>
            </div>
            <div class="info-trace-navigation">
              <button id="info-trace-back" class="button" type="button" title="Remove the latest hop" disabled>← Back</button>
              <span id="info-trace-depth" class="info-trace-depth">Select a target</span>
              <button id="info-trace-forward" class="button" type="button" title="Add the next hop" disabled>Next →</button>
            </div>
            <p id="info-trace-status" class="muted small">Open this tab to load the document trace graph.</p>
            <div id="info-trace-steps" class="info-trace-steps"></div>
          </div>

          <div id="tab-panel-source" class="inspector-tab-panel" role="tabpanel" hidden>
            <p class="eyebrow">Source mapping</p>
            <h2 id="selection-title" class="section-title">No object selected</h2>
            <div id="selection-meta" class="meta-list"></div>
            <div id="source-comment" class="source-comment"></div>
            <pre id="source-code" class="source-code empty">Click a region in the PDF viewer to show the matching PDF source snippet here.</pre>
            <div class="vector-owner-search">
              <button id="vector-owner-search" class="button" type="button" disabled>
                Search vector ownership
              </button>
              <div id="vector-owner-result" class="vector-owner-result muted small">
                Select a vector to search its extracted ownership.
              </div>
            </div>
            <div id="reference-chain" class="reference-chain"></div>
            <div id="warnings" class="warnings"></div>
          </div>
        </div>
      </aside>
    </main>
  </div>
  <dialog id="hyperlink-confirm-dialog" class="hyperlink-confirm-dialog" aria-labelledby="hyperlink-confirm-title">
    <div class="hyperlink-confirm-card">
      <p class="eyebrow">Hyperlink navigation</p>
      <h2 id="hyperlink-confirm-title" class="section-title">Follow hyperlink?</h2>
      <p id="hyperlink-confirm-summary" class="hyperlink-confirm-summary"></p>
      <div id="hyperlink-confirm-meta" class="meta-list hyperlink-confirm-meta"></div>
      <div class="hyperlink-confirm-actions">
        <button id="hyperlink-confirm-cancel" class="button hyperlink-cancel-button" type="button">Cancel</button>
        <button id="hyperlink-confirm-follow" class="button hyperlink-follow-button" type="button">
          Go to page and highlight
        </button>
      </div>
    </div>
  </dialog>
  <dialog id="info-trace-confirm-dialog" class="hyperlink-confirm-dialog" aria-labelledby="info-trace-confirm-title">
    <div class="hyperlink-confirm-card">
      <p class="eyebrow">Info Trace navigation</p>
      <h2 id="info-trace-confirm-title" class="section-title">Open traced item?</h2>
      <p id="info-trace-confirm-summary" class="hyperlink-confirm-summary"></p>
      <div id="info-trace-confirm-meta" class="meta-list hyperlink-confirm-meta"></div>
      <div class="hyperlink-confirm-actions">
        <button id="info-trace-confirm-cancel" class="button hyperlink-cancel-button" type="button">Cancel</button>
        <button id="info-trace-confirm-follow" class="button hyperlink-follow-button" type="button">
          Go to page and highlight
        </button>
      </div>
    </div>
  </dialog>
  <dialog id="info-trace-stop-dialog" class="hyperlink-confirm-dialog" aria-labelledby="info-trace-stop-title">
    <div class="hyperlink-confirm-card">
      <p class="eyebrow">Info Trace</p>
      <h2 id="info-trace-stop-title" class="section-title">Stop current trace?</h2>
      <p class="hyperlink-confirm-summary">All hops and trace highlights will be cleared. This action cannot be undone.</p>
      <div class="hyperlink-confirm-actions">
        <button id="info-trace-stop-cancel" class="button hyperlink-cancel-button" type="button">Cancel</button>
        <button id="info-trace-stop-confirm" class="button info-trace-danger" type="button">Stop Trace</button>
      </div>
    </div>
  </dialog>
  <dialog id="large-page-warning-dialog" class="hyperlink-confirm-dialog" aria-labelledby="large-page-warning-title">
    <div class="hyperlink-confirm-card">
      <p class="eyebrow">Performance warning</p>
      <h2 id="large-page-warning-title" class="section-title">Large page ahead</h2>
      <p id="large-page-warning-summary" class="hyperlink-confirm-summary"></p>
      <div class="hyperlink-confirm-actions large-page-warning-actions">
        <button id="large-page-warning-cancel" class="button hyperlink-cancel-button" type="button">Cancel</button>
        <button id="large-page-warning-confirm" class="button hyperlink-follow-button" type="button">Load vectors anyway</button>
        <button id="large-page-warning-skip-vectors" class="button extract-info-button" type="button">
          Open without vectors
        </button>
      </div>
    </div>
  </dialog>
`

const docSelect = mustQuery<HTMLSelectElement>('#doc-select')
const pageSelect = mustQuery<HTMLSelectElement>('#page-select')
const prevPageButton = mustQuery<HTMLButtonElement>('#prev-page')
const nextPageButton = mustQuery<HTMLButtonElement>('#next-page')
const docMeta = mustQuery<HTMLDivElement>('#doc-meta')
const pageMeta = mustQuery<HTMLDivElement>('#page-meta')
const viewerStatus = mustQuery<HTMLDivElement>('#viewer-status')
const viewerScroll = mustQuery<HTMLDivElement>('#viewer-scroll')
const viewerStage = mustQuery<HTMLDivElement>('#viewer-stage')
const playgroundCode = mustQuery<HTMLTextAreaElement>('#playground-code')
const playgroundStatus = mustQuery<HTMLParagraphElement>('#playground-status')
const playgroundBoard = mustQuery<HTMLDivElement>('#playground-board')
const playgroundCanvas = mustQuery<HTMLCanvasElement>('#playground-canvas')
const playgroundEmpty = mustQuery<HTMLDivElement>('#playground-empty')
const playgroundCoords = mustQuery<HTMLDivElement>('#playground-coords')
const playgroundFitBtn = mustQuery<HTMLButtonElement>('#playground-fit')
const playgroundResetBtn = mustQuery<HTMLButtonElement>('#playground-reset')
const missingVectorInput = mustQuery<HTMLInputElement>('#missing-vector-input')
const scaleMinInput = mustQuery<HTMLInputElement>('#scale-min-input')
const scaleMaxInput = mustQuery<HTMLInputElement>('#scale-max-input')
const areaSelectToggleBtn = mustQuery<HTMLButtonElement>('#area-select-toggle')
const splitPageDetectBtn = mustQuery<HTMLButtonElement>('#split-page-detect')
const entityDetectBtn = mustQuery<HTMLButtonElement>('#entity-detect')
const boxDetectBtn = mustQuery<HTMLButtonElement>('#box-detect')
const circleDetectBtn = mustQuery<HTMLButtonElement>('#circle-detect')
const dashedDetectBtn = mustQuery<HTMLButtonElement>('#dashed-detect')
const cellDetectBtn = mustQuery<HTMLButtonElement>('#cell-detect')
const pinCircleDetectBtn = mustQuery<HTMLButtonElement>('#pin-circle-detect')
const pinArrowDetectBtn = mustQuery<HTMLButtonElement>('#pin-arrow-detect')
const wireMarkDetectBtn = mustQuery<HTMLButtonElement>('#wire-mark-detect')
const symbolMatchDisplayBtn = mustQuery<HTMLButtonElement>('#symbol-match-display')
const vectorMatchSearchBtn = mustQuery<HTMLButtonElement>('#vector-match-search')
const vectorMatchResultsSelect = mustQuery<HTMLSelectElement>('#vector-match-results')
const zoomInput = mustQuery<HTMLInputElement>('#zoom-input')
const zoomLabel = mustQuery<HTMLSpanElement>('#zoom-label')
const selectionTitle = mustQuery<HTMLHeadingElement>('#selection-title')
const selectionMeta = mustQuery<HTMLDivElement>('#selection-meta')
const sourceComment = mustQuery<HTMLDivElement>('#source-comment')
const sourceStashBtn = mustQuery<HTMLButtonElement>('#source-stash')
const playgroundStashCount = mustQuery<HTMLSpanElement>('#playground-stash-count')
const sourceCompareBtn = mustQuery<HTMLButtonElement>('#source-compare')
const playgroundShapeScaleInput = mustQuery<HTMLInputElement>('#playground-shape-scale')
const sourceCode = mustQuery<HTMLPreElement>('#source-code')
const vectorOwnerSearchBtn = mustQuery<HTMLButtonElement>('#vector-owner-search')
const vectorOwnerResult = mustQuery<HTMLDivElement>('#vector-owner-result')
const referenceChain = mustQuery<HTMLDivElement>('#reference-chain')
const warningsEl = mustQuery<HTMLDivElement>('#warnings')
const hyperlinkConfirmDialog = mustQuery<HTMLDialogElement>('#hyperlink-confirm-dialog')
const hyperlinkConfirmSummary = mustQuery<HTMLParagraphElement>('#hyperlink-confirm-summary')
const hyperlinkConfirmMeta = mustQuery<HTMLDivElement>('#hyperlink-confirm-meta')
const hyperlinkConfirmCancelBtn = mustQuery<HTMLButtonElement>('#hyperlink-confirm-cancel')
const hyperlinkConfirmFollowBtn = mustQuery<HTMLButtonElement>('#hyperlink-confirm-follow')

hyperlinkConfirmCancelBtn.addEventListener('click', () => {
  pendingHyperlinkNavigation = null
  hyperlinkConfirmDialog.close()
})
hyperlinkConfirmDialog.addEventListener('cancel', () => {
  pendingHyperlinkNavigation = null
})
hyperlinkConfirmFollowBtn.addEventListener('click', () => {
  const item = pendingHyperlinkNavigation
  pendingHyperlinkNavigation = null
  hyperlinkConfirmDialog.close()
  if (item) {
    void followHyperlink(item)
  }
})

const LARGE_PAGE_VECTOR_THRESHOLD = 6000
const largePageWarningDialog = mustQuery<HTMLDialogElement>('#large-page-warning-dialog')
const largePageWarningSummary = mustQuery<HTMLParagraphElement>('#large-page-warning-summary')
const largePageWarningCancelBtn = mustQuery<HTMLButtonElement>('#large-page-warning-cancel')
const largePageWarningSkipVectorsBtn = mustQuery<HTMLButtonElement>('#large-page-warning-skip-vectors')
const largePageWarningConfirmBtn = mustQuery<HTMLButtonElement>('#large-page-warning-confirm')

type LargePageDecision = 'cancel' | 'skip-vectors' | 'full'

largePageWarningCancelBtn.addEventListener('click', () => {
  largePageWarningDialog.returnValue = 'cancel'
  largePageWarningDialog.close()
})
largePageWarningDialog.addEventListener('cancel', () => {
  largePageWarningDialog.returnValue = 'cancel'
})
largePageWarningSkipVectorsBtn.addEventListener('click', () => {
  largePageWarningDialog.returnValue = 'skip-vectors'
  largePageWarningDialog.close()
})
largePageWarningConfirmBtn.addEventListener('click', () => {
  largePageWarningDialog.returnValue = 'full'
  largePageWarningDialog.close()
})

// Set by confirmLargePageNavigation() when the user opts to skip the vector
// overlay for a specific page. Persists across re-renders of that same page
// (zoom, resize, layer toggles) so the perf win isn't silently undone; it is
// cleared whenever navigation lands on a different page.
let vectorOverlaySuppressedPage: number | null = null

function confirmLargePageNavigation(pageNumber: number): Promise<LargePageDecision> {
  const targetPage = getPageManifest(pageNumber)
  const vectorCount = countOrZero(targetPage?.item_counts.vector_path)
  if (vectorCount <= LARGE_PAGE_VECTOR_THRESHOLD) {
    return Promise.resolve('full')
  }
  largePageWarningSummary.textContent =
    `Page ${pageNumber} contains ${vectorCount.toLocaleString()} vector objects (over ${LARGE_PAGE_VECTOR_THRESHOLD.toLocaleString()}), ` +
    'which may make the viewer lag while rendering. You can open it without the vector overlay for a fast, ' +
    'read-only view of the page image, or load everything anyway.'
  return new Promise((resolve) => {
    const onClose = () => {
      largePageWarningDialog.removeEventListener('close', onClose)
      resolve(largePageWarningDialog.returnValue as LargePageDecision)
    }
    largePageWarningDialog.addEventListener('close', onClose)
    largePageWarningDialog.showModal()
    largePageWarningCancelBtn.focus()
  })
}
const inspectorTabs = Array.from(
  document.querySelectorAll<HTMLButtonElement>('.inspector-tab'),
)
const symbolPagesInput = mustQuery<HTMLInputElement>('#symbol-pages-input')
const symbolAddPageBtn = mustQuery<HTMLButtonElement>('#symbol-add-page')
const symbolExtractBtn = mustQuery<HTMLButtonElement>('#symbol-extract')
const symbolSearchBtn = mustQuery<HTMLButtonElement>('#symbol-search')
const symbolStatus = mustQuery<HTMLParagraphElement>('#symbol-status')
const symbolList = mustQuery<HTMLDivElement>('#symbol-list')
const extractSymbolDependency = mustQuery<HTMLParagraphElement>('#extract-symbol-dependency')
const extractLlmGateInput = mustQuery<HTMLInputElement>('#extract-llm-gate')
const extractInfoRunBtn = mustQuery<HTMLButtonElement>('#extract-info-run')
const extractInfoCancelBtn = mustQuery<HTMLButtonElement>('#extract-info-cancel')
const extractInfoStatus = mustQuery<HTMLParagraphElement>('#extract-info-status')
const extractProgressBar = mustQuery<HTMLDivElement>('#extract-progress-bar')
const extractInfoResult = mustQuery<HTMLDivElement>('#extract-info-result')
const extractJsonSearchCategory = mustQuery<HTMLSelectElement>('#extract-json-search-category')
const extractJsonSearchId = mustQuery<HTMLInputElement>('#extract-json-search-id')
const extractJsonSearchButton = mustQuery<HTMLButtonElement>('#extract-json-search-button')
const extractJsonSearchStatus = mustQuery<HTMLSpanElement>('#extract-json-search-status')
const infoTracePageInput = mustQuery<HTMLInputElement>('#info-trace-page')
const infoTraceKindSelect = mustQuery<HTMLSelectElement>('#info-trace-kind')
const infoTraceIdSelect = mustQuery<HTMLSelectElement>('#info-trace-id')
const infoTraceChooseBtn = mustQuery<HTMLButtonElement>('#info-trace-choose')
const infoTraceStopBtn = mustQuery<HTMLButtonElement>('#info-trace-stop')
const infoTraceBackBtn = mustQuery<HTMLButtonElement>('#info-trace-back')
const infoTraceForwardBtn = mustQuery<HTMLButtonElement>('#info-trace-forward')
const infoTraceDepth = mustQuery<HTMLSpanElement>('#info-trace-depth')
const infoTraceStatus = mustQuery<HTMLParagraphElement>('#info-trace-status')
const infoTraceSteps = mustQuery<HTMLDivElement>('#info-trace-steps')
const infoTraceConfirmDialog = mustQuery<HTMLDialogElement>('#info-trace-confirm-dialog')
const infoTraceConfirmSummary = mustQuery<HTMLParagraphElement>('#info-trace-confirm-summary')
const infoTraceConfirmMeta = mustQuery<HTMLDivElement>('#info-trace-confirm-meta')
const infoTraceConfirmCancelBtn = mustQuery<HTMLButtonElement>('#info-trace-confirm-cancel')
const infoTraceConfirmFollowBtn = mustQuery<HTMLButtonElement>('#info-trace-confirm-follow')
const infoTraceStopDialog = mustQuery<HTMLDialogElement>('#info-trace-stop-dialog')
const infoTraceStopCancelBtn = mustQuery<HTMLButtonElement>('#info-trace-stop-cancel')
const infoTraceStopConfirmBtn = mustQuery<HTMLButtonElement>('#info-trace-stop-confirm')

type VectorMatcherSelection = {
  pageNumber: number
  queryBBox: BBox
  bbox: BBox
  shapeCount: number
  shapes: unknown[]
  sourceCode: string
  itemIds: string[]
}

type PlaygroundStash = {
  pageNumber: number
  queryBBox: BBox
  shapes: unknown[]
  sourceCode: string
}

type VectorMatchResult = {
  page_number: number
  bbox_pdf: BBox
  shape_count: number
  anchor?: unknown
}

type ApiVectorMatchResult = {
  page_number: number
  bbox_pdf: BBox
  vectors?: unknown[]
  shape_count?: number
  anchor?: unknown
}

type VectorEntityGroup = {
  root: number
  indices: number[]
  vectors: ApiPathBase[]
  bbox_pdf: BBox
  bbox_mupdf?: BBox
  size: number
}

type VectorEntityResult = {
  page_number: number
  entity_count: number
  groups: VectorEntityGroup[]
}

type SplitPageResult = {
  page_number: number
  page_bbox: BBox
  content_bbox: BBox
  info_bbox: BBox | null
}

type VectorBoxGroup = {
  bbox_pdf: BBox
  bbox_mupdf?: BBox
  vector_indices: number[]
  width: number
  height: number
  // Polygon outline in PyMuPDF (top-left origin) coordinates; screen = point * scale.
  points?: Array<[number, number]>
  direction?: 'up' | 'down' | 'left' | 'right'
}

type VectorBoxResult = {
  page_number: number
  entity_root: number
  entity_index: number
  box_count: number
  boxes: VectorBoxGroup[]
}

type VectorCircleResult = {
  page_number: number
  entity_root: number
  entity_index: number
  circle_count: number
  circles: VectorBoxGroup[]
}

type VectorDashedResult = {
  page_number: number
  entity_root: number
  entity_index: number
  dashed_count: number
  dashed: VectorBoxGroup[]
}

type VectorCellResult = {
  page_number: number
  entity_root: number
  entity_index: number
  cell_count: number
  cells: VectorBoxGroup[]
}

type ApiVectorEntityGroup = {
  category: string
  bbox: BBox
  vectors: ApiPathBase[]
  points: Array<[number, number]>
  polygon?: unknown
}

type VectorPinResult = {
  page_number: number
  entity_root: number
  entity_index: number
  kind: 'circle' | 'arrow' | 'wire_mark'
  pin_count: number
  pins: VectorBoxGroup[]
}

type ApiPathBase = {
  index?: number
  type?: string
  code?: string
  points: Array<[number, number]>
  path_meta?: Record<string, unknown>
  bbox?: BBox
}

type HyperlinkTargetHighlight = {
  pageNumber: number
  region: NonNullable<NonNullable<LinkItem['link']['target']>['target_highlight_region']>
  sourceItemId: string
}

type ExtractedElement = {
  id: number
  type: string
  shape: ApiPathBase[]
  vector_indices: number[]
  image?: string | null
  title?: string[]
  descriptions?: string[]
  [key: string]: unknown
}

type ExtractedEntity = {
  id: number
  type: string
  page: number
  bbox: Partial<BBox> | null
  title: string[]
  descriptions: string[]
  elements: Array<number | string>
  // Non-enumerable frontend projections resolved from diagram.elements.
  shape: ApiPathBase[]
  vector_indices: number[]
  [key: string]: unknown
}

type ExtractedComponent = ExtractedEntity & { endpoints: Array<number | string> }
type ExtractedGroup = ExtractedEntity
type ExtractedWire = ExtractedEntity & {
  vectors: ApiPathBase[]
}
type ExtractedEndpoint = ExtractedEntity
type ExtractedNet = ExtractedEntity & {
  wire_ids: Array<number | string>
  endpoints: Array<number | string>
}

type ExtractedRelation = {
  type: string
  source: string
  target: string
  [key: string]: unknown
}

type ExtractedHyperlink = {
  source_page: number
  source_bbox?: Pick<BBox, 'x0' | 'y0' | 'x1' | 'y1'>
  source_component: number | string | null
  target_page: number
  target_bbox: Pick<BBox, 'x0' | 'y0' | 'x1' | 'y1'> | null
  target_component?: number | string
}

type ExtractedCrosspageRelations = {
  hyperlinks: ExtractedHyperlink[]
  transfers: ExtractedHyperlink[]
}

type InfoTraceKind = 'component' | 'wire'

type InfoTraceEntity = {
  id: number | string
  type?: string
  page?: number
  bbox: Partial<BBox> | null
  title?: string[]
  descriptions?: string[]
}

type InfoTracePageIndex = {
  page_number: number
  components: InfoTraceEntity[]
  wires: InfoTraceEntity[]
  endpoints: InfoTraceEntity[]
  nets?: InfoTraceEntity[]
  relations: ExtractedRelation[]
}

type InfoTraceIndex = {
  category: 'trace_index'
  pages: InfoTracePageIndex[]
  transfers: ExtractedHyperlink[]
  document_result_filename?: string | null
}

type InfoTraceNode = {
  page: number
  kind: InfoTraceKind
  id: number | string
  bbox: Partial<BBox> | null
  title: string[]
  descriptions: string[]
}

type InfoTraceStep = {
  depth: number
  nodes: InfoTraceNode[]
  endpoints: Array<{ page: number; id: number | string }>
  nets: Array<{ page: number; id: number | string }>
  selectedNodeKeys: string[]
}

type InfoTraceOpenEndpoint = {
  page: number
  id: number | string
  sources: InfoTraceNode[]
  candidates: InfoTraceNode[]
}

type InfoTraceEndpointState = {
  open: InfoTraceOpenEndpoint[]
  resolved: Array<{ page: number; id: number | string }>
}

type ExtractedTextOwnership = {
  text_indices: number[]
  component_id?: number | string
  endpoint_id?: number | string
}

type ExtractedInfoTable = {
  html: string
  cells: Array<Record<string, unknown>>
  rows: unknown[]
  columns: unknown[]
  raw_cells: Array<Record<string, unknown>>
  [key: string]: unknown
}

type ExtractInfoResult = {
  category: 'extract_info'
  page_number: number
  diagram: ExtractedDiagram
  crosspage_relations: ExtractedCrosspageRelations
  info_table: ExtractedInfoTable
  _text_ownership?: ExtractedTextOwnership[]
  // Non-enumerable frontend projections for interaction code.
  components: ExtractedComponent[]
  elements: ExtractedElement[]
  groups: ExtractedGroup[]
  wires: ExtractedWire[]
  endpoints: ExtractedEndpoint[]
  relations: ExtractedRelation[]
  nets: ExtractedNet[]
  [key: string]: unknown
}

type ExtractedDiagram = {
  elements: ExtractedElement[]
  components: ExtractedComponent[]
  endpoints: ExtractedEndpoint[]
  wires: ExtractedWire[]
  nets: ExtractedNet[]
  groups: ExtractedGroup[]
  relations: ExtractedRelation[]
  remaining_vectors?: ApiPathBase[]
  remaining_text?: Array<Record<string, unknown>>
}

type ApiVectorBoxGroup = {
  category: string
  bbox: BBox
  vectors: ApiPathBase[]
  points: Array<[number, number]>
  polygon?: unknown
  direction?: 'up' | 'down' | 'left' | 'right'
}

type PageDetectionCache = {
  splitPage?: SplitPageResult
  entities?: VectorEntityResult
  boxes: Map<number, VectorBoxResult>
  circles: Map<number, VectorCircleResult>
  dashed: Map<number, VectorDashedResult>
  cells: Map<number, VectorCellResult>
  pinCircles: Map<number, VectorPinResult>
  pinArrows: Map<number, VectorPinResult>
  wireMarks: Map<number, VectorPinResult>
  symbolSearches: Map<string, SymbolSearchResult>
}

const pageCache = new Map<string, PageData>()
const pdfDocumentCache = new Map<string, Promise<Awaited<ReturnType<typeof loadPdfDocument>>>>()
const detectionCache = new Map<string, PageDetectionCache>()
let manifest: Manifest | null = null
let activeDocument: ReaderDocument | null = null
let activePageNumber = 1
let activeSelectionId: string | null = null
let hyperlinkTargetHighlight: HyperlinkTargetHighlight | null = null
let pendingHyperlinkNavigation: LinkItem | null = null
let infoTraceIndexData: InfoTraceIndex | null = null
let infoTraceIndexDocumentId: string | null = null
let infoTraceIndexPromise: Promise<InfoTraceIndex | null> | null = null
let infoTraceAllSteps: InfoTraceStep[] = []
let infoTraceOpenEndpoints: InfoTraceOpenEndpoint[] = []
let infoTraceResolvedEndpoints: Array<{ page: number; id: number | string }> = []
let pendingInfoTraceNavigation: InfoTraceNode | null = null
let activeVectorMatcherSelection: VectorMatcherSelection | null = null
let currentPageState: PageState | null = null
let vectorMatchResults: VectorMatchResult[] = []
let vectorMatchResultsLabel = 'Results: None'
let splitPageResult: SplitPageResult | null = null
let vectorEntityResult: VectorEntityResult | null = null
let activeVectorEntityIndex: number | null = null
let vectorBoxResult: VectorBoxResult | null = null
let activeVectorBoxIndex: number | null = null
let vectorCircleResult: VectorCircleResult | null = null
let activeVectorCircleIndex: number | null = null
let vectorDashedResult: VectorDashedResult | null = null
let activeVectorDashedIndex: number | null = null
let vectorCellResult: VectorCellResult | null = null
let activeVectorCellIndex: number | null = null
let vectorPinCircleResult: VectorPinResult | null = null
let vectorPinArrowResult: VectorPinResult | null = null
let vectorWireMarkResult: VectorPinResult | null = null
let symbolMatchBoxes: SymbolMatchBox[] = []
let activeSymbolHighlight: number | null = null
let showSymbolMatches = true
let lastExtractedPages: number[] = []
let lastExtractedSymbolCount = 0
let hasSymbolExtractionResult = false
let isSymbolSearchRunning = false
let extractInfoResultData: ExtractInfoResult | null = null
let extractTextOwnership: ExtractedTextOwnership[] = []
let activeExtractHighlightVectors: ApiPathBase[] = []
let relatedExtractHighlightVectors: ApiPathBase[] = []
let activeExtractHighlightEndpoints: ExtractedEndpoint[] = []
let activeExtractHighlightTexts: BBox[] = []
let extractInfoAbortController: AbortController | null = null
let isExtractInfoRunning = false
let extractInfoCancellationRequested = false
let selectedVectorGroupItemIds = new Set<string>()
let isAreaSelectMode = false
let areaSelectionBox: HTMLDivElement | null = null
let areaDragStart: { x: number; y: number } | null = null
let areaDragPointerId: number | null = null
let zoomFactor = 1
let fitScale = 1
let pendingZoomAnchor: ZoomAnchor | null = null
let renderSequence = 0
let scheduledRenderTimer: number | null = null
let isPanning = false
let panStartX = 0
let panStartY = 0
let panScrollLeft = 0
let panScrollTop = 0
let panStartTranslateX = 0
let panStartTranslateY = 0
// Extra translation applied to the page stage on an axis that is smaller than
// the viewport (no scroll range), so zoom/pan stay anchored to the cursor
// instead of snapping to the top-left corner.
let stageTranslateX = 0
let stageTranslateY = 0
let playgroundData: PlaygroundData | null = null
let playgroundView: PlaygroundView = { scale: 1, offsetX: 0, offsetY: 0 }
let playgroundHover: PlaygroundHover | null = null
let playgroundComparison: PlaygroundComparison | null = null
let playgroundShapeScale = 1
let playgroundStashedVectorCount = 0
let playgroundStash: PlaygroundStash | null = null
let pendingPlaygroundSource = ''
let playgroundPanPointerId: number | null = null
let playgroundPanStartX = 0
let playgroundPanStartY = 0
let playgroundPanOffsetX = 0
let playgroundPanOffsetY = 0
let layoutSizes = loadLayoutSizes()

const MIN_ZOOM_FACTOR = 0.5
const MAX_ZOOM_FACTOR = 4
const PLAYGROUND_MIN_SCALE = 0.02
const PLAYGROUND_MAX_SCALE = 200

function getDetectionCacheKey(): string | null {
  return activeDocument ? `${activeDocument.id}:${activePageNumber}` : null
}

function getPageDetectionCache(create = false): PageDetectionCache | null {
  const key = getDetectionCacheKey()
  if (!key) {
    return null
  }
  let cached = detectionCache.get(key)
  if (!cached && create) {
    cached = {
      splitPage: undefined,
      boxes: new Map(),
      circles: new Map(),
      dashed: new Map(),
      cells: new Map(),
      pinCircles: new Map(),
      pinArrows: new Map(),
      wireMarks: new Map(),
      symbolSearches: new Map(),
    }
    detectionCache.set(key, cached)
  }
  return cached ?? null
}

function escapeHtml(text: string): string {
  return text
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

function renderMeta(container: HTMLElement, items: Array<[string, string]>): void {
  if (!items.length) {
    container.innerHTML = ''
    return
  }
  container.innerHTML = items
    .map(([label, value]) => {
      const normalizedLabel = label.toLowerCase()
      const toneClass = /resolved|loaded|extracted|complete/.test(normalizedLabel)
        ? ' is-success-metric'
        : /link|warning|missing|count|pages?/.test(normalizedLabel)
          ? ' is-summary-metric'
          : /endpoint|pin|relation|reference/.test(normalizedLabel)
            ? ' is-special-metric'
            : ''
      return `<div class="meta-row${toneClass}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
    })
    .join('')
}

function formatJson(value: unknown): string {
  if (value == null) {
    return 'null'
  }
  if (typeof value === 'string') {
    return value
  }
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function highlightSource(source: ReaderSource): string {
  const start = Math.max(0, Math.min(source.highlight_start, source.snippet.length))
  const end = Math.max(start, Math.min(source.highlight_end, source.snippet.length))
  const before = escapeHtml(source.snippet.slice(0, start))
  const hit = escapeHtml(source.snippet.slice(start, end))
  const after = escapeHtml(source.snippet.slice(end))
  return `${before}<mark>${hit || ' '}</mark>${after}`
}

function getPageManifest(pageNumber: number): PageManifest | undefined {
  return activeDocument?.pages.find((page) => page.page_number === pageNumber)
}

function pageKey(documentId: string, pageNumber: number): string {
  return `${documentId}:${pageNumber}`
}

async function loadManifest(): Promise<Manifest> {
  const response = await fetch('/reader-data/manifest.json')
  if (!response.ok) {
    throw new Error(`Failed to load manifest: ${response.status}`)
  }
  return (await response.json()) as Manifest
}

async function loadPdfDocument(url: string) {
  const task = getDocument(url)
  return await task.promise
}

function normalizePageItems(
  data: Omit<PageData, 'items' | 'components'> & { items: Array<ReaderItem | ComponentItem> },
): PageData {
  return {
    ...data,
    items: data.items.filter((item): item is ReaderItem => isReaderItem(item)),
    components: data.items.filter((item): item is ComponentItem => isComponentItem(item)),
  }
}

async function loadPageData(documentId: string, page: PageManifest): Promise<PageData> {
  const key = pageKey(documentId, page.page_number)
  if (pageCache.has(key)) {
    return pageCache.get(key) as PageData
  }
  const response = await fetch(page.data_url)
  if (!response.ok) {
    throw new Error(`Failed to load page data: ${response.status}`)
  }
  const raw = (await response.json()) as Omit<PageData, 'items' | 'components'> & {
    items: Array<ReaderItem | ComponentItem>
  }
  const data = normalizePageItems(raw)
  pageCache.set(key, data)
  return data
}

async function getPdfDocument(url: string) {
  if (!pdfDocumentCache.has(url)) {
    pdfDocumentCache.set(url, loadPdfDocument(url))
  }
  return await (pdfDocumentCache.get(url) as Promise<Awaited<ReturnType<typeof loadPdfDocument>>>)
}

function setStatus(message: string): void {
  viewerStatus.textContent = message
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max)
}

function applyLayoutSizes(): void {
  document.documentElement.style.setProperty('--sidebar-width', `${layoutSizes.sidebarWidth}px`)
  document.documentElement.style.setProperty('--inspector-width', `${layoutSizes.inspectorWidth}px`)
  document.documentElement.style.setProperty('--viewer-height', `${layoutSizes.viewerHeight}px`)
  document.documentElement.style.setProperty('--playground-editor-width', `${layoutSizes.playgroundEditorWidth}px`)
}

function refreshAfterLayoutResize(): void {
  renderPlayground()
  if (activeDocument) {
    scheduleRender(activePageNumber, { preserveSelection: true, anchor: getViewportCenterAnchor() })
  }
}

function initResizableLayout(): void {
  applyLayoutSizes()

  document.querySelectorAll<HTMLElement>('.resize-handle[data-resize-target]').forEach((handle) => {
    handle.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) {
        return
      }
      event.preventDefault()
      event.stopPropagation()

      const target = handle.dataset.resizeTarget
      const startX = event.clientX
      const startY = event.clientY
      const startSizes = { ...layoutSizes }
      handle.classList.add('is-resizing')
      handle.setPointerCapture(event.pointerId)

      const move = (moveEvent: PointerEvent) => {
        const dx = moveEvent.clientX - startX
        const dy = moveEvent.clientY - startY
        if (target === 'sidebar') {
          layoutSizes.sidebarWidth = clamp(startSizes.sidebarWidth + dx, 240, 520)
        } else if (target === 'inspector') {
          layoutSizes.inspectorWidth = clamp(startSizes.inspectorWidth - dx, 320, 720)
        } else if (target === 'viewer') {
          layoutSizes.viewerHeight = clamp(startSizes.viewerHeight + dy, 420, 1200)
        } else if (target === 'playground-editor') {
          layoutSizes.playgroundEditorWidth = clamp(startSizes.playgroundEditorWidth + dx, 260, 560)
        }
        applyLayoutSizes()
        renderPlayground()
      }

      const stop = () => {
        handle.classList.remove('is-resizing')
        handle.removeEventListener('pointermove', move)
        handle.removeEventListener('pointerup', stop)
        handle.removeEventListener('pointercancel', stop)
        saveLayoutSizes(layoutSizes)
        refreshAfterLayoutResize()
      }

      handle.addEventListener('pointermove', move)
      handle.addEventListener('pointerup', stop)
      handle.addEventListener('pointercancel', stop)
    })
  })
}

function updateZoomLabel(): void {
  const zoomPercent = Math.round(zoomFactor * 100)
  zoomInput.value = String(zoomPercent)
  zoomLabel.textContent = Math.abs(zoomFactor - 1) < 0.001 ? 'Fit width' : `${zoomPercent}% of fit width`
}

function countOrZero(value: number | undefined): number {
  return typeof value === 'number' ? value : 0
}

function getAvailableViewerWidth(): number {
  const style = window.getComputedStyle(viewerScroll)
  const padding =
    Number.parseFloat(style.paddingLeft || '0') + Number.parseFloat(style.paddingRight || '0')
  return Math.max((viewerScroll.clientWidth || 960) - padding - 28, 320)
}

function scheduleRender(pageNumber: number, options: RenderPageOptions = {}): void {
  if (scheduledRenderTimer !== null) {
    window.clearTimeout(scheduledRenderTimer)
  }
  scheduledRenderTimer = window.setTimeout(() => {
    scheduledRenderTimer = null
    void renderPage(pageNumber, options)
  }, 40)
}

function applyStageTransform(): void {
  viewerStage.style.transform =
    stageTranslateX === 0 && stageTranslateY === 0
      ? ''
      : `translate(${stageTranslateX}px, ${stageTranslateY}px)`
}

function getScrollMax(): { x: number; y: number } {
  // Measure native scroll extents with the stage transform cleared, so a
  // translate applied on an underflowing axis does not masquerade as scroll
  // range. The toggle is synchronous (no paint between), so it is invisible.
  const prevX = stageTranslateX
  const prevY = stageTranslateY
  if (prevX !== 0 || prevY !== 0) {
    stageTranslateX = 0
    stageTranslateY = 0
    applyStageTransform()
  }
  const max = {
    x: Math.max(viewerScroll.scrollWidth - viewerScroll.clientWidth, 0),
    y: Math.max(viewerScroll.scrollHeight - viewerScroll.clientHeight, 0),
  }
  if (prevX !== 0 || prevY !== 0) {
    stageTranslateX = prevX
    stageTranslateY = prevY
    applyStageTransform()
  }
  return max
}

function getPageContentOffset(): { x: number; y: number } {
  // Constant offset (viewer + shell padding, page header) between the scroll
  // content origin and the page canvas top-left. It does not scale with zoom,
  // so it must be removed before converting to document coordinates and added
  // back when restoring scroll, otherwise the zoom anchor drifts toward the
  // top-left corner.
  const canvas = currentPageState?.canvas
  if (!canvas) {
    return { x: 0, y: 0 }
  }
  const canvasRect = canvas.getBoundingClientRect()
  const viewerRect = viewerScroll.getBoundingClientRect()
  return {
    x: canvasRect.left - viewerRect.left + viewerScroll.scrollLeft,
    y: canvasRect.top - viewerRect.top + viewerScroll.scrollTop,
  }
}

function getViewportAnchorFromClientPoint(clientX: number, clientY: number): ZoomAnchor | null {
  const pageState = currentPageState
  // renderPage temporarily removes the canvas. Computing an anchor during
  // that gap uses a zero content offset (and often reset scroll positions),
  // which incorrectly moves the next render to the document's top-left.
  if (!pageState || !pageState.canvas.isConnected) {
    return null
  }
  const rect = viewerScroll.getBoundingClientRect()
  const viewportX = clientX - rect.left
  const viewportY = clientY - rect.top
  if (viewportX < 0 || viewportY < 0 || viewportX > rect.width || viewportY > rect.height) {
    return null
  }
  const scale = pageState.scale
  if (scale <= 0) {
    return null
  }
  const offset = getPageContentOffset()
  return {
    documentX: (viewerScroll.scrollLeft + viewportX - offset.x) / scale,
    documentY: (viewerScroll.scrollTop + viewportY - offset.y) / scale,
    viewportX,
    viewportY,
  }
}

function getViewportCenterAnchor(): ZoomAnchor | null {
  const pageState = currentPageState
  if (!pageState || !pageState.canvas.isConnected) {
    return null
  }
  const rect = viewerScroll.getBoundingClientRect()
  if (!rect.width || !rect.height) {
    return null
  }
  const scale = pageState.scale
  if (scale <= 0) {
    return null
  }
  const offset = getPageContentOffset()
  const viewportX = rect.width / 2
  const viewportY = rect.height / 2
  return {
    documentX: (viewerScroll.scrollLeft + viewportX - offset.x) / scale,
    documentY: (viewerScroll.scrollTop + viewportY - offset.y) / scale,
    viewportX,
    viewportY,
  }
}

function applyAnchorScroll(anchor: ZoomAnchor | null, scale: number): void {
  if (!anchor) {
    stageTranslateX = 0
    stageTranslateY = 0
    applyStageTransform()
    viewerScroll.scrollLeft = 0
    viewerScroll.scrollTop = 0
    return
  }

  // Canvas offset within the scroll content, minus the active translate, gives
  // the fixed padding/header offset (scroll- and zoom-invariant).
  const offset = getPageContentOffset()
  const baseX = offset.x - stageTranslateX
  const baseY = offset.y - stageTranslateY

  // Clear the transform so scroll extents reflect the real content size.
  stageTranslateX = 0
  stageTranslateY = 0
  applyStageTransform()
  const maxScrollLeft = Math.max(viewerScroll.scrollWidth - viewerScroll.clientWidth, 0)
  const maxScrollTop = Math.max(viewerScroll.scrollHeight - viewerScroll.clientHeight, 0)

  // Screen position the canvas top-left must take so the anchored document
  // point lands back under its original viewport position.
  const desiredX = anchor.viewportX - anchor.documentX * scale
  const desiredY = anchor.viewportY - anchor.documentY * scale

  // canvasScreen = base + translate - scroll. Overflow axes use scroll (clamped
  // to the edges); underflow axes use translate (free, so the cursor anchor is
  // kept even with blank space around the page).
  if (maxScrollLeft > 0) {
    viewerScroll.scrollLeft = clamp(baseX - desiredX, 0, maxScrollLeft)
  } else {
    viewerScroll.scrollLeft = 0
    stageTranslateX = desiredX - baseX
  }
  if (maxScrollTop > 0) {
    viewerScroll.scrollTop = clamp(baseY - desiredY, 0, maxScrollTop)
  } else {
    viewerScroll.scrollTop = 0
    stageTranslateY = desiredY - baseY
  }
  applyStageTransform()
}

function setPlaygroundStatus(message: string): void {
  playgroundStatus.textContent = message
}

function getPlaygroundCanvasSize() {
  return {
    width: Math.max(playgroundBoard.clientWidth, 320),
    height: Math.max(playgroundBoard.clientHeight, 240),
  }
}

function computePlaygroundBounds(polylines: PlaygroundData['polylines']): PlaygroundBounds | null {
  const points = polylines.flatMap((polyline) => polyline.points)
  if (!points.length) {
    return null
  }

  let minX = Number.POSITIVE_INFINITY
  let minY = Number.POSITIVE_INFINITY
  let maxX = Number.NEGATIVE_INFINITY
  let maxY = Number.NEGATIVE_INFINITY

  for (const [x, y] of points) {
    minX = Math.min(minX, x)
    minY = Math.min(minY, y)
    maxX = Math.max(maxX, x)
    maxY = Math.max(maxY, y)
  }

  return {
    minX,
    minY,
    maxX,
    maxY,
    width: Math.max(maxX - minX, 1),
    height: Math.max(maxY - minY, 1),
  }
}

function normalizePolylinesToOrigin(
  polylines: PlaygroundData['polylines'],
  bounds: PlaygroundBounds,
): PlaygroundData['polylines'] {
  return polylines.map((polyline) => ({
    closed: polyline.closed,
    points: polyline.points.map(([x, y]) => [x - bounds.minX, bounds.maxY - y] as [number, number]),
  }))
}

function parsePlaygroundSnippet(snippet: string): PlaygroundData | null {
  const commands = parseSnippetToPathCommands(snippet)
  const sourcePolylines = commandsToPolylines(commands).filter((polyline) => polyline.points.length >= 2)
  if (!sourcePolylines.length) {
    return null
  }

  const sourceBounds = computePlaygroundBounds(sourcePolylines)
  if (!sourceBounds) {
    return null
  }

  const polylines = normalizePolylinesToOrigin(sourcePolylines, sourceBounds)
  const bounds = computePlaygroundBounds(polylines)
  if (!bounds) {
    return null
  }

  return { sourceBounds, polylines, bounds }
}

function fitPlaygroundView(): void {
  if (!playgroundData) {
    playgroundView = { scale: 1, offsetX: 0, offsetY: 0 }
    renderPlayground()
    return
  }

  const visibleBounds = getVisiblePlaygroundBounds()
  if (!visibleBounds) return
  const { width, height } = getPlaygroundCanvasSize()
  const padding = 32
  const fitScale = Math.min(
    Math.max((width - padding * 2) / visibleBounds.width, PLAYGROUND_MIN_SCALE),
    Math.max((height - padding * 2) / visibleBounds.height, PLAYGROUND_MIN_SCALE),
  )
  const scale = clamp(fitScale, PLAYGROUND_MIN_SCALE, PLAYGROUND_MAX_SCALE)
  playgroundView = {
    scale,
    offsetX: (width - visibleBounds.width * scale) / 2 - visibleBounds.minX * scale,
    offsetY: (height + visibleBounds.height * scale) / 2 + visibleBounds.minY * scale,
  }
  renderPlayground()
}

function screenToPlaygroundWorld(screenX: number, screenY: number): { x: number; y: number } {
  return {
    x: (screenX - playgroundView.offsetX) / playgroundView.scale,
    y: (playgroundView.offsetY - screenY) / playgroundView.scale,
  }
}

function worldToPlaygroundScreen(x: number, y: number): { x: number; y: number } {
  return {
    x: x * playgroundView.scale + playgroundView.offsetX,
    y: playgroundView.offsetY - y * playgroundView.scale,
  }
}

function scalePlaygroundPolylines(
  polylines: PlaygroundData['polylines'],
  scale: number,
): PlaygroundData['polylines'] {
  if (Math.abs(scale - 1) < 1e-12) return polylines
  return polylines.map((polyline) => ({
    closed: polyline.closed,
    points: polyline.points.map(([x, y]) => [x * scale, y * scale] as [number, number]),
  }))
}

function getVisiblePlaygroundBounds(): PlaygroundBounds | null {
  const polylines: PlaygroundData['polylines'] = []
  if (playgroundData) {
    polylines.push(...scalePlaygroundPolylines(playgroundData.polylines, playgroundShapeScale))
  }
  if (playgroundComparison) {
    polylines.push(...playgroundComparison.candidate.polylines)
  }
  return computePlaygroundBounds(polylines)
}

function playgroundWorldToSource(data: PlaygroundData, x: number, y: number): { x: number; y: number } {
  return {
    x: x + data.sourceBounds.minX,
    y: data.sourceBounds.maxY - y,
  }
}

function collectPlaygroundPoints(
  data: PlaygroundData | null,
  label: string,
  color: string,
  renderScale = 1,
): PlaygroundPoint[] {
  if (!data) {
    return []
  }
  const points: PlaygroundPoint[] = []
  for (let polylineIndex = 0; polylineIndex < data.polylines.length; polylineIndex += 1) {
    const polyline = data.polylines[polylineIndex]
    for (let pointIndex = 0; pointIndex < polyline.points.length; pointIndex += 1) {
      const [x, y] = polyline.points[pointIndex]
      const source = playgroundWorldToSource(data, x, y)
      points.push({
        x: x * renderScale,
        y: y * renderScale,
        sourceX: source.x,
        sourceY: source.y,
        label: `${label} ${polylineIndex + 1}.${pointIndex + 1}`,
        color,
      })
    }
  }
  return points
}

function getVisiblePlaygroundPoints(): PlaygroundPoint[] {
  const points = collectPlaygroundPoints(playgroundData, 'shape', '#718e78', playgroundShapeScale)
  if (playgroundComparison) {
    points.push(...collectPlaygroundPoints(playgroundComparison.candidate, 'compare', '#b28a4a'))
  }
  return points
}

function findNearestPlaygroundPoint(screenX: number, screenY: number): PlaygroundPoint | null {
  let nearest: { point: PlaygroundPoint; distance: number } | null = null
  for (const point of getVisiblePlaygroundPoints()) {
    const screen = worldToPlaygroundScreen(point.x, point.y)
    const distance = Math.hypot(screen.x - screenX, screen.y - screenY)
    if (!nearest || distance < nearest.distance) {
      nearest = { point, distance }
    }
  }
  const snapDistance = 12
  return nearest && nearest.distance <= snapDistance ? nearest.point : null
}

function getPlaygroundStep(scale: number): number {
  const targetPx = 88
  const rawStep = targetPx / scale
  const exponent = Math.floor(Math.log10(Math.max(rawStep, 1e-6)))
  const base = 10 ** exponent
  const multiples = [1, 2, 5, 10]
  for (const multiple of multiples) {
    const step = base * multiple
    if (step >= rawStep) {
      return step
    }
  }
  return base * 10
}

function polylineDistance(
  a: PlaygroundData['polylines'][number],
  b: PlaygroundData['polylines'][number],
  reversed = false,
): number {
  if (a.points.length !== b.points.length) {
    return Number.POSITIVE_INFINITY
  }
  let maxDistance = 0
  for (let index = 0; index < a.points.length; index += 1) {
    const pointA = a.points[index]
    const pointB = b.points[reversed ? b.points.length - 1 - index : index]
    maxDistance = Math.max(maxDistance, Math.hypot(pointA[0] - pointB[0], pointA[1] - pointB[1]))
  }
  return maxDistance
}

function arePolylinesMatching(a: PlaygroundData['polylines'][number], b: PlaygroundData['polylines'][number]): boolean {
  if (a.closed !== b.closed || a.points.length !== b.points.length) {
    return false
  }
  const tolerance = 0.75
  return Math.min(polylineDistance(a, b), polylineDistance(a, b, true)) <= tolerance
}

function comparePlaygroundData(base: PlaygroundData, candidate: PlaygroundData): PlaygroundComparison {
  const matchedBaseIndexes = new Set<number>()
  const matchedCandidateIndexes = new Set<number>()

  for (let candidateIndex = 0; candidateIndex < candidate.polylines.length; candidateIndex += 1) {
    const candidatePolyline = candidate.polylines[candidateIndex]
    for (let baseIndex = 0; baseIndex < base.polylines.length; baseIndex += 1) {
      if (matchedBaseIndexes.has(baseIndex)) {
        continue
      }
      if (arePolylinesMatching(base.polylines[baseIndex], candidatePolyline)) {
        matchedBaseIndexes.add(baseIndex)
        matchedCandidateIndexes.add(candidateIndex)
        break
      }
    }
  }

  return { candidate, matchedBaseIndexes, matchedCandidateIndexes }
}

function updateSourceCompareButton(): void {
  const canCompare = Boolean(playgroundData && activeVectorMatcherSelection?.sourceCode)
  sourceCompareBtn.hidden = !canCompare
  sourceCompareBtn.disabled = !canCompare
  sourceCompareBtn.classList.toggle('is-active', Boolean(playgroundComparison))
  sourceCompareBtn.textContent = playgroundComparison ? 'Cancel Compare' : 'Compare'
}

function renderPlayground(): void {
  const context = playgroundCanvas.getContext('2d')
  if (!context) {
    return
  }

  const { width, height } = getPlaygroundCanvasSize()
  const dpr = window.devicePixelRatio || 1
  const pixelWidth = Math.max(1, Math.round(width * dpr))
  const pixelHeight = Math.max(1, Math.round(height * dpr))
  if (playgroundCanvas.width !== pixelWidth || playgroundCanvas.height !== pixelHeight) {
    playgroundCanvas.width = pixelWidth
    playgroundCanvas.height = pixelHeight
  }
  playgroundCanvas.style.width = `${width}px`
  playgroundCanvas.style.height = `${height}px`

  context.setTransform(dpr, 0, 0, dpr, 0, 0)
  context.clearRect(0, 0, width, height)

  const background = context.createLinearGradient(0, 0, 0, height)
  background.addColorStop(0, '#faf9f7')
  background.addColorStop(1, '#f5f3f0')
  context.fillStyle = background
  context.fillRect(0, 0, width, height)

  const scale = Math.max(playgroundView.scale, PLAYGROUND_MIN_SCALE)
  const step = getPlaygroundStep(scale)
  const majorStep = step * 5
  const worldTopLeft = screenToPlaygroundWorld(0, 0)
  const worldBottomRight = screenToPlaygroundWorld(width, height)
  const minX = Math.min(worldTopLeft.x, worldBottomRight.x)
  const maxX = Math.max(worldTopLeft.x, worldBottomRight.x)
  const minY = Math.min(worldTopLeft.y, worldBottomRight.y)
  const maxY = Math.max(worldTopLeft.y, worldBottomRight.y)

  const drawGrid = (gridStep: number, strokeStyle: string, lineWidth: number) => {
    context.beginPath()
    for (let x = Math.floor(minX / gridStep) * gridStep; x <= maxX + gridStep; x += gridStep) {
      const screen = worldToPlaygroundScreen(x, 0)
      context.moveTo(screen.x, 0)
      context.lineTo(screen.x, height)
    }
    for (let y = Math.floor(minY / gridStep) * gridStep; y <= maxY + gridStep; y += gridStep) {
      const screen = worldToPlaygroundScreen(0, y)
      context.moveTo(0, screen.y)
      context.lineTo(width, screen.y)
    }
    context.strokeStyle = strokeStyle
    context.lineWidth = lineWidth
    context.stroke()
  }

  drawGrid(step, '#e1ddd7', 1)
  drawGrid(majorStep, '#cec9c1', 1)

  const axisX = worldToPlaygroundScreen(0, 0).x
  const axisY = worldToPlaygroundScreen(0, 0).y
  context.beginPath()
  if (axisX >= 0 && axisX <= width) {
    context.moveTo(axisX, 0)
    context.lineTo(axisX, height)
  }
  if (axisY >= 0 && axisY <= height) {
    context.moveTo(0, axisY)
    context.lineTo(width, axisY)
  }
  context.strokeStyle = 'rgba(83, 108, 141, 0.7)'
  context.lineWidth = 1.5
  context.stroke()

  const drawPolylines = (
    polylines: PlaygroundData['polylines'],
    colorForIndex: (index: number) => string,
    alpha = 1,
  ) => {
    context.save()
    context.globalAlpha = alpha
    context.lineWidth = Math.max(1.5, Math.min(3, 2.2 / Math.sqrt(scale / 2)))
    context.lineJoin = 'round'
    context.lineCap = 'round'

    for (let polylineIndex = 0; polylineIndex < polylines.length; polylineIndex += 1) {
      const polyline = polylines[polylineIndex]
      const first = polyline.points[0]
      if (!first) {
        continue
      }
      context.strokeStyle = colorForIndex(polylineIndex)
      const start = worldToPlaygroundScreen(first[0], first[1])
      context.beginPath()
      context.moveTo(start.x, start.y)
      for (let index = 1; index < polyline.points.length; index += 1) {
        const point = polyline.points[index]
        const screen = worldToPlaygroundScreen(point[0], point[1])
        context.lineTo(screen.x, screen.y)
      }
      if (polyline.closed) {
        context.closePath()
      }
      context.stroke()
    }
    context.restore()
  }

  if (playgroundData) {
    drawPolylines(scalePlaygroundPolylines(playgroundData.polylines, playgroundShapeScale), (index) =>
      playgroundComparison?.matchedBaseIndexes.has(index) ? '#536c8d' : '#718e78',
    )
  }

  if (playgroundComparison) {
    drawPolylines(
      playgroundComparison.candidate.polylines,
      (index) => (playgroundComparison?.matchedCandidateIndexes.has(index) ? '#536c8d' : '#b28a4a'),
      0.95,
    )
  }

  const drawPoints = (points: PlaygroundPoint[]) => {
    context.save()
    for (const point of points) {
      const screen = worldToPlaygroundScreen(point.x, point.y)
      if (screen.x < -8 || screen.x > width + 8 || screen.y < -8 || screen.y > height + 8) {
        continue
      }
      context.beginPath()
      context.arc(screen.x, screen.y, 3.6, 0, Math.PI * 2)
      context.fillStyle = '#ffffff'
      context.fill()
      context.lineWidth = 1.5
      context.strokeStyle = point.color
      context.stroke()
    }
    context.restore()
  }

  drawPoints(getVisiblePlaygroundPoints())

  if (playgroundHover) {
    const hoverScreen = worldToPlaygroundScreen(playgroundHover.x, playgroundHover.y)
    context.save()
    context.setLineDash([6, 6])
    context.beginPath()
    context.moveTo(hoverScreen.x, 0)
    context.lineTo(hoverScreen.x, height)
    context.moveTo(0, hoverScreen.y)
    context.lineTo(width, hoverScreen.y)
    context.strokeStyle = 'rgba(102, 112, 133, 0.38)'
    context.lineWidth = 1
    context.stroke()
    context.restore()

    if (playgroundHover.snapped) {
      const text = `${playgroundHover.label}: ${playgroundHover.sourceX.toFixed(2)}, ${playgroundHover.sourceY.toFixed(2)}`
      context.save()
      context.setLineDash([])
      context.beginPath()
      context.arc(hoverScreen.x, hoverScreen.y, 6, 0, Math.PI * 2)
      context.fillStyle = 'rgba(83, 108, 141, 0.12)'
      context.fill()
      context.lineWidth = 2
      context.strokeStyle = '#536c8d'
      context.stroke()

      context.font = '12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'
      const metrics = context.measureText(text)
      const labelWidth = metrics.width + 14
      const labelHeight = 24
      const labelX = Math.min(Math.max(hoverScreen.x + 10, 8), width - labelWidth - 8)
      const labelY = Math.min(Math.max(hoverScreen.y - 32, 8), height - labelHeight - 8)
      context.fillStyle = 'rgba(255, 255, 255, 0.96)'
      context.strokeStyle = 'rgba(83, 108, 141, 0.45)'
      context.lineWidth = 1
      context.beginPath()
      context.roundRect(labelX, labelY, labelWidth, labelHeight, 6)
      context.fill()
      context.stroke()
      context.fillStyle = '#344054'
      context.fillText(text, labelX + 7, labelY + 16)
      context.restore()
    }
  }
}

function updatePlaygroundHover(event: PointerEvent | MouseEvent): void {
  const rect = playgroundBoard.getBoundingClientRect()
  const x = clamp(event.clientX - rect.left, 0, rect.width)
  const y = clamp(event.clientY - rect.top, 0, rect.height)
  const snapped = findNearestPlaygroundPoint(x, y)
  const world = snapped ? { x: snapped.x, y: snapped.y } : screenToPlaygroundWorld(x, y)
  const source = snapped
    ? { x: snapped.sourceX, y: snapped.sourceY }
    : playgroundData
      ? playgroundWorldToSource(playgroundData, world.x, world.y)
      : world
  playgroundHover = {
    x: world.x,
    y: world.y,
    sourceX: source.x,
    sourceY: source.y,
    snapped: Boolean(snapped),
    label: snapped?.label ?? 'cursor',
  }
  playgroundCoords.textContent = playgroundHover.snapped
    ? `${playgroundHover.label} | x: ${playgroundHover.sourceX.toFixed(2)}, y: ${playgroundHover.sourceY.toFixed(2)}`
    : `x: ${playgroundHover.sourceX.toFixed(2)}, y: ${playgroundHover.sourceY.toFixed(2)}`
  renderPlayground()
}

function clearPlaygroundHover(): void {
  playgroundHover = null
  playgroundCoords.textContent = 'x: -, y: -'
  renderPlayground()
}

function refreshPlayground(autoFit = true): void {
  const nextData = parsePlaygroundSnippet(playgroundCode.value)
  playgroundData = nextData
  playgroundComparison = null
  playgroundEmpty.hidden = Boolean(nextData)
  playgroundShapeScaleInput.disabled = !nextData
  updateVectorMatchButton()
  updateSourceCompareButton()

  if (!playgroundCode.value.trim()) {
    playgroundData = null
    playgroundEmpty.hidden = false
    setPlaygroundStatus('Paste vector path code to draw it on the whiteboard below.')
    updateSourceCompareButton()
    renderPlayground()
    return
  }

  if (!nextData) {
    setPlaygroundStatus('Unable to parse vector commands. Use PDF path operators such as m, l, c, re, h.')
    updateSourceCompareButton()
    renderPlayground()
    return
  }

  setPlaygroundStatus(
    `Rendered ${nextData.polylines.length} path${nextData.polylines.length === 1 ? '' : 's'} aligned to the lower-left origin.`,
  )
  if (autoFit) {
    fitPlaygroundView()
  } else {
    renderPlayground()
  }
}

function clearPlayground(): void {
  playgroundCode.value = ''
  playgroundData = null
  playgroundComparison = null
  playgroundStash = null
  playgroundShapeScale = 1
  playgroundShapeScaleInput.value = '100'
  playgroundShapeScaleInput.disabled = true
  updatePlaygroundStashCount(0)
  playgroundView = { scale: 1, offsetX: 0, offsetY: 0 }
  playgroundEmpty.hidden = false
  vectorMatchResults = []
  vectorMatchResultsLabel = 'Results: None'
  renderVectorMatchResultsSelect()
  renderVectorMatchOverlays()
  updateVectorMatchButton()
  updateSourceCompareButton()
  setPlaygroundStatus('Playground cleared. Select a shape and use Stash Shape to preview it here.')
  renderPlayground()
}

function zoomPlaygroundAt(clientX: number, clientY: number, factor: number): void {
  const rect = playgroundBoard.getBoundingClientRect()
  const screenX = clamp(clientX - rect.left, 0, rect.width)
  const screenY = clamp(clientY - rect.top, 0, rect.height)
  const anchor = screenToPlaygroundWorld(screenX, screenY)
  const nextScale = clamp(playgroundView.scale * factor, PLAYGROUND_MIN_SCALE, PLAYGROUND_MAX_SCALE)
  playgroundView.scale = nextScale
  playgroundView.offsetX = screenX - anchor.x * nextScale
  playgroundView.offsetY = screenY + anchor.y * nextScale
  renderPlayground()
}

function initPlayground(): void {
  const resizeObserver = new ResizeObserver(() => {
    if (playgroundData) {
      fitPlaygroundView()
      return
    }
    renderPlayground()
  })
  resizeObserver.observe(playgroundBoard)

  let inputTimer: number | null = null
  playgroundCode.addEventListener('input', () => {
    if (inputTimer !== null) {
      window.clearTimeout(inputTimer)
    }
    inputTimer = window.setTimeout(() => {
      inputTimer = null
      refreshPlayground(true)
    }, 220)
  })

  playgroundFitBtn.addEventListener('click', () => {
    fitPlaygroundView()
  })

  playgroundResetBtn.addEventListener('click', () => {
    clearPlayground()
  })

  playgroundBoard.addEventListener(
    'wheel',
    (event) => {
      event.preventDefault()
      const factor = Math.exp(-event.deltaY * 0.0015)
      zoomPlaygroundAt(event.clientX, event.clientY, factor)
      updatePlaygroundHover(event)
    },
    { passive: false },
  )

  playgroundBoard.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) {
      return
    }
    playgroundPanPointerId = event.pointerId
    playgroundPanStartX = event.clientX
    playgroundPanStartY = event.clientY
    playgroundPanOffsetX = playgroundView.offsetX
    playgroundPanOffsetY = playgroundView.offsetY
    playgroundBoard.classList.add('is-panning')
    playgroundBoard.setPointerCapture(event.pointerId)
  })

  playgroundBoard.addEventListener('pointermove', (event) => {
    updatePlaygroundHover(event)
    if (playgroundPanPointerId !== event.pointerId) {
      return
    }
    const deltaX = event.clientX - playgroundPanStartX
    const deltaY = event.clientY - playgroundPanStartY
    playgroundView.offsetX = playgroundPanOffsetX + deltaX
    playgroundView.offsetY = playgroundPanOffsetY + deltaY
    renderPlayground()
  })

  const stopPlaygroundPan = (event: PointerEvent) => {
    if (playgroundPanPointerId !== event.pointerId) {
      return
    }
    playgroundPanPointerId = null
    playgroundBoard.classList.remove('is-panning')
  }

  playgroundBoard.addEventListener('pointerup', stopPlaygroundPan)
  playgroundBoard.addEventListener('pointercancel', stopPlaygroundPan)
  playgroundBoard.addEventListener('pointerleave', () => {
    clearPlaygroundHover()
  })
  playgroundBoard.addEventListener('mouseenter', () => {
    renderPlayground()
  })

  renderPlayground()
}

function renderReferenceChain(item: ReaderItem, pageData: PageData): void {
  if (!item.reference_chain.length) {
    referenceChain.innerHTML = ''
    return
  }

  referenceChain.innerHTML = [
    '<div class="reference-section-title">Reference chain</div>',
    ...item.reference_chain.map((entry) => {
      const detail = pageData.object_details[entry.object_ref]
      const description = detail
        ? `<p class="reference-description"><strong>${escapeHtml(detail.kind_label)}.</strong> ${escapeHtml(detail.description)}</p>`
        : '<p class="reference-description">Object details are not available for this reference.</p>'
      const rawSource = detail?.raw_source
        ? `<pre class="reference-source">${escapeHtml(detail.raw_source)}</pre>`
        : '<div class="reference-empty">No raw object preview available.</div>'
      const decodedPreview = detail?.decoded_stream_preview
        ? `
          <details class="reference-details">
            <summary>Decoded stream preview</summary>
            <pre class="reference-source">${escapeHtml(detail.decoded_stream_preview)}</pre>
          </details>
        `
        : ''

      return `
        <section class="reference-card">
          <div class="reference-card-header">
            <strong>${escapeHtml(entry.object_ref)}</strong>
            <span>${escapeHtml(entry.role)}</span>
          </div>
          ${description}
          ${rawSource}
          ${decodedPreview}
        </section>
      `
    }),
  ].join('')
}

function clearVectorMatcherSelection(): void {
  activeVectorMatcherSelection = null
  playgroundComparison = null
  vectorMatchResults = []
  vectorMatchResultsLabel = 'Results: None'
  selectedVectorGroupItemIds = new Set()
  setPendingPlaygroundSource('')
  renderVectorMatchResultsSelect()
  renderSplitPageOverlays()
  renderVectorMatchOverlays()
  renderVectorEntityOverlays()
  renderVectorBoxOverlays()
  renderVectorCircleOverlays()
  renderVectorDashedOverlays()
  renderVectorCellOverlays()
  renderVectorPinOverlays()
  updateVectorMatchButton()
  updateSourceCompareButton()
  syncSelectionClasses()
}

function clearSplitPageResult(): void {
  splitPageResult = null
  updateDetectButtons()
  renderSplitPageOverlays()
}

function clearVectorEntityResult(): void {
  vectorEntityResult = null
  activeVectorEntityIndex = null
  clearVectorBoxResult()
  clearVectorCircleResult()
  clearVectorDashedResult()
  clearVectorCellResult()
  clearVectorPinResult()
  updateVectorEntityActionButtons()
  updateDetectButtons()
  renderVectorEntityOverlays()
}

function clearVectorBoxResult(): void {
  vectorBoxResult = null
  activeVectorBoxIndex = null
  updateDetectButtons()
  renderVectorBoxOverlays()
}

function clearVectorCircleResult(): void {
  vectorCircleResult = null
  activeVectorCircleIndex = null
  updateDetectButtons()
  renderVectorCircleOverlays()
}

function clearVectorDashedResult(): void {
  vectorDashedResult = null
  activeVectorDashedIndex = null
  updateDetectButtons()
  renderVectorDashedOverlays()
}

function clearVectorCellResult(): void {
  vectorCellResult = null
  activeVectorCellIndex = null
  updateDetectButtons()
  renderVectorCellOverlays()
}

function clearVectorPinResult(kind?: 'circle' | 'arrow' | 'wire_mark'): void {
  if (!kind || kind === 'circle') vectorPinCircleResult = null
  if (!kind || kind === 'arrow') vectorPinArrowResult = null
  if (!kind || kind === 'wire_mark') vectorWireMarkResult = null
  updateDetectButtons()
  renderVectorPinOverlays()
}

function setPendingPlaygroundSource(source: string): void {
  pendingPlaygroundSource = source.trim()
  sourceStashBtn.disabled = !pendingPlaygroundSource
}

function updatePlaygroundStashCount(count: number): void {
  playgroundStashedVectorCount = Math.max(0, Math.floor(count))
  playgroundStashCount.textContent = `Stashed: ${playgroundStashedVectorCount} vector${playgroundStashedVectorCount === 1 ? '' : 's'}`
}

function setVectorMatcherSelection(selection: VectorMatcherSelection | null, preserveMatches = false): void {
  const previousSourceCode = activeVectorMatcherSelection?.sourceCode ?? ''
  activeVectorMatcherSelection = selection
  selectedVectorGroupItemIds = new Set(selection?.itemIds ?? [])
  setPendingPlaygroundSource(selection?.sourceCode ?? '')
  if (!selection || selection.sourceCode !== previousSourceCode) {
    playgroundComparison = null
  }
  if (!preserveMatches) {
    vectorMatchResults = []
    vectorMatchResultsLabel = 'Results: None'
  }
  renderVectorMatchResultsSelect()
  renderVectorMatchOverlays()
  renderVectorEntityOverlays()
  renderVectorBoxOverlays()
  renderVectorCircleOverlays()
  renderVectorDashedOverlays()
  renderVectorCellOverlays()
  updateVectorMatchButton()
  updateSourceCompareButton()

  if (!selection) {
    syncSelectionClasses()
    return
  }

  activeSelectionId = null
  setInspectorTab('source')
  selectionTitle.textContent = `${selection.shapeCount} Vector Shapes Selected`
  vectorOwnerSearchBtn.disabled = true
  vectorOwnerResult.textContent = 'Ownership search is available for one clicked PDF vector at a time.'
  sourceCode.classList.remove('empty')
  sourceCode.textContent = selection.sourceCode || 'The selected vector shape group is stored in memory for pdf_parser matching.'
  setPendingPlaygroundSource(selection.sourceCode)
  sourceComment.innerHTML =
    '<div class="source-note">Selected source is ready to stash. Click Stash Shape in the playground toolbar to preview and search this vector group.</div>'
  warningsEl.innerHTML = ''
  referenceChain.innerHTML = ''
  renderMeta(selectionMeta, [
    ['Page', String(selection.pageNumber)],
    ['Shapes', String(selection.shapeCount)],
    ['Highlighted items', String(selection.itemIds.length)],
    ['BBox', `${selection.bbox.x0}, ${selection.bbox.y0}, ${selection.bbox.x1}, ${selection.bbox.y1}`],
  ])
  syncSelectionClasses()
}

function setSelection(item: ReaderItem | null, pageData: PageData | null): void {
  if (isInfoTraceActive()) {
    activeSelectionId = null
    syncSelectionClasses()
    renderInfoTraceHighlights()
    return
  }
  activeVectorMatcherSelection = null
  playgroundComparison = null
  vectorMatchResults = []
  vectorMatchResultsLabel = 'Results: None'
  selectedVectorGroupItemIds = new Set()
  renderVectorMatchResultsSelect()
  renderVectorMatchOverlays()
  renderVectorEntityOverlays()
  renderVectorBoxOverlays()
  renderVectorCircleOverlays()
  renderVectorDashedOverlays()
  renderVectorCellOverlays()
  updateVectorMatchButton()
  updateSourceCompareButton()
  activeSelectionId = item?.id ?? null
  selectionMeta.innerHTML = ''
  sourceComment.innerHTML = ''
  warningsEl.innerHTML = ''
  referenceChain.innerHTML = ''
  setPendingPlaygroundSource('')
  vectorOwnerSearchBtn.disabled = item?.kind !== 'vector_path'
  vectorOwnerResult.textContent = item?.kind === 'vector_path'
    ? 'Click Search vector ownership to inspect the latest extraction result.'
    : 'Select a vector to search its extracted ownership.'

  if (!item || !pageData) {
    selectionTitle.textContent = 'No object selected'
    sourceCode.classList.add('empty')
    sourceCode.textContent = 'Click a region in the PDF viewer to show the matching PDF source snippet here.'
    syncSelectionClasses()
    return
  }

  const titleByKind: Record<ReaderItem['kind'], string> = {
    vector_path: 'Vector object',
    text: 'Text object',
    image: 'Image object',
    link: 'Hyperlink',
  }
  setInspectorTab('extract-info')
  if (!focusExtractInfoForReaderItem(item, pageData)) {
    extractJsonSearchStatus.textContent = extractInfoResultData
      ? 'The selected PDF item has no extracted component ownership.'
      : 'Run Extract Info to resolve the selected PDF item.'
  }
  selectionTitle.textContent = `${titleByKind[item.kind]} ${item.id}`
  sourceCode.classList.remove('empty')
  if (item.kind === 'text') {
    sourceCode.textContent = formatJson(item)
  } else {
    sourceCode.innerHTML = highlightSource(item.source)
  }
  setPendingPlaygroundSource(item.kind === 'vector_path' ? item.source.snippet : '')
  sourceComment.innerHTML = item.source_comment
    ? `<div class="source-note">${escapeHtml(item.source_comment)}</div>`
    : ''

  const metaRows: Array<[string, string]> = [
    ['Page', String(item.page_number)],
    ['Object ref', item.source.object_ref ?? 'None'],
    ['Content stream', pageData.content_streams.join(', ') || 'None'],
  ]

  if (item.kind === 'vector_path') {
    metaRows.push(['Type', 'Vector path'])
    metaRows.push(['Paint op', item.paint_operator])
    metaRows.push(['Command count', String(item.summary.command_count)])
    metaRows.push(['Point count', String(item.summary.point_count)])
  } else if (item.kind === 'link') {
    metaRows.push(['Type', 'Link annotation'])
    metaRows.push(['Link kind', item.link.kind ?? 'unknown'])
  } else if (item.kind === 'text') {
    metaRows.push(['Type', 'Text block'])
    metaRows.push(['Text', item.text.content])
    metaRows.push(['Text op', item.text.operator])
    metaRows.push(['Font', item.text.font ?? 'unknown'])
    metaRows.push(['Font size', String(item.text.font_size)])
    metaRows.push(['Chars', String(item.summary.char_count)])
    metaRows.push(['Decoded via ToUnicode', item.text.decoded_via_tounicode ? 'Yes' : 'No'])
    if (item.text.raw_glyph_text) {
      metaRows.push(['Raw glyph text', item.text.raw_glyph_text])
    }
  } else if (item.kind === 'image') {
    metaRows.push(['Type', 'Image XObject'])
    metaRows.push(['Image name', item.image.name])
    metaRows.push(['Image object', item.image.object_ref])
    metaRows.push(['Pixel size', `${item.image.pixel_width ?? '?'} x ${item.image.pixel_height ?? '?'}`])
    metaRows.push(['Filters', item.image.filters.join(', ') || 'None'])
    metaRows.push(['Draw size (pt)', `${item.summary.draw_width} x ${item.summary.draw_height}`])
  }

  metaRows.push(['BBox', `${item.bbox.x0}, ${item.bbox.y0}, ${item.bbox.x1}, ${item.bbox.y1}`])
  renderMeta(selectionMeta, metaRows)
  renderReferenceChain(item, pageData)

  if (item.kind === 'link') {
    warningsEl.innerHTML = `
      <div class="warning-card">
        <strong>Navigation target</strong>
        <pre>${escapeHtml(formatJson(item.link.target ?? item.link.action))}</pre>
      </div>
    `
  } else if (item.kind === 'text' && item.text.raw_glyph_text) {
    warningsEl.innerHTML = `
      <div class="warning-card">
        <strong>Raw glyph text</strong>
        <pre>${escapeHtml(item.text.raw_glyph_text)}</pre>
      </div>
    `
  } else if (pageData.warnings.length) {
    warningsEl.innerHTML = pageData.warnings
      .map((warning) => `<div class="warning-card">${escapeHtml(warning)}</div>`)
      .join('')
  }

  syncSelectionClasses()
}

function syncSelectionClasses(): void {
  document.querySelectorAll<HTMLElement>('.overlay-item').forEach((node) => {
    const itemId = node.dataset.itemId ?? ''
    const isSelected = itemId === activeSelectionId
    const isGroupSelected = selectedVectorGroupItemIds.has(itemId)
    node.classList.toggle('is-selected', isSelected)
    node.classList.toggle('is-group-selected', isGroupSelected)
    node.style.zIndex = isSelected ? '10' : isGroupSelected ? '9' : node.dataset.baseZIndex ?? '1'
  })
}

function scrollToBBoxCenter(bbox: BBox): void {
  const st = currentPageState
  if (!st) {
    return
  }
  const pageHeight = st.pageData.page_size.height_pt
  const scale = st.scale
  const cx = ((bbox.x0 + bbox.x1) / 2) * scale
  const cy = (pageHeight - (bbox.y0 + bbox.y1) / 2) * scale
  const viewportW = viewerScroll.clientWidth
  const viewportH = viewerScroll.clientHeight
  viewerScroll.scrollTo({
    left: Math.max(cx - viewportW / 2, 0),
    top: Math.max(cy - viewportH / 2, 0),
    behavior: 'smooth',
  })
}

function updateVectorMatchButton(): void {
  const canSearch = Boolean(playgroundStash) && Boolean(activeDocument) && Boolean(currentPageState)
  vectorMatchSearchBtn.disabled = !canSearch
}

function getSelectedVectorEntityGroup(): VectorEntityGroup | null {
  const selectedGroup =
    vectorEntityResult && activeVectorEntityIndex !== null
      ? vectorEntityResult.groups[activeVectorEntityIndex]
      : null
  return selectedGroup ?? null
}

function updateVectorEntityActionButtons(): void {
  const selectedGroup = getSelectedVectorEntityGroup()
  boxDetectBtn.disabled = !selectedGroup
  circleDetectBtn.disabled = !selectedGroup
  dashedDetectBtn.disabled = !selectedGroup
  cellDetectBtn.disabled = !selectedGroup
  pinCircleDetectBtn.disabled = !selectedGroup
  pinArrowDetectBtn.disabled = !selectedGroup
  wireMarkDetectBtn.disabled = !selectedGroup
  updateSymbolSearchButton()
  updateDetectButtons()
}

function updateSymbolSearchButton(): void {
  symbolSearchBtn.disabled =
    isSymbolSearchRunning ||
    !activeDocument ||
    !getSelectedVectorEntityGroup() ||
    !lastExtractedPages.length ||
    lastExtractedSymbolCount === 0
}

function updateExtractInfoAvailability(): void {
  const ready = Boolean(activeDocument && hasSymbolExtractionResult && lastExtractedPages.length)
  extractInfoRunBtn.disabled = !ready || isExtractInfoRunning
  extractInfoCancelBtn.disabled = !isExtractInfoRunning || extractInfoCancellationRequested
  extractSymbolDependency.textContent = ready
    ? `✓ Symbols ready · page${lastExtractedPages.length === 1 ? '' : 's'} ${lastExtractedPages.join(', ')}`
    : 'Run extraction in the Symbols tab first.'
  extractSymbolDependency.classList.toggle('is-ready', ready)
}

function updateDetectButtons(): void {
  const splitPageActive = Boolean(splitPageResult && splitPageResult.page_number === activePageNumber)
  const entityActive = Boolean(vectorEntityResult && vectorEntityResult.page_number === activePageNumber)
  const selectedGroup = getSelectedVectorEntityGroup()
  const boxActive = Boolean(
    selectedGroup &&
      vectorBoxResult &&
      vectorBoxResult.page_number === activePageNumber &&
      vectorBoxResult.entity_root === selectedGroup.root,
  )
  const circleActive = Boolean(
    selectedGroup &&
      vectorCircleResult &&
      vectorCircleResult.page_number === activePageNumber &&
      vectorCircleResult.entity_root === selectedGroup.root,
  )
  const dashedActive = Boolean(
    selectedGroup &&
      vectorDashedResult &&
      vectorDashedResult.page_number === activePageNumber &&
      vectorDashedResult.entity_root === selectedGroup.root,
  )
  const cellActive = Boolean(
    selectedGroup &&
      vectorCellResult &&
      vectorCellResult.page_number === activePageNumber &&
      vectorCellResult.entity_root === selectedGroup.root,
  )
  const pinCircleActive = Boolean(
    selectedGroup &&
      vectorPinCircleResult?.page_number === activePageNumber &&
      vectorPinCircleResult.entity_root === selectedGroup.root,
  )
  const pinArrowActive = Boolean(
    selectedGroup &&
      vectorPinArrowResult?.page_number === activePageNumber &&
      vectorPinArrowResult.entity_root === selectedGroup.root,
  )
  const wireMarkActive = Boolean(
    selectedGroup &&
      vectorWireMarkResult?.page_number === activePageNumber &&
      vectorWireMarkResult.entity_root === selectedGroup.root,
  )

  splitPageDetectBtn.classList.toggle('is-active', splitPageActive)
  splitPageDetectBtn.setAttribute('aria-pressed', String(splitPageActive))

  entityDetectBtn.classList.toggle('is-active', entityActive)
  entityDetectBtn.setAttribute('aria-pressed', String(entityActive))

  boxDetectBtn.classList.toggle('is-active', boxActive)
  boxDetectBtn.setAttribute('aria-pressed', String(boxActive))

  circleDetectBtn.classList.toggle('is-active', circleActive)
  circleDetectBtn.setAttribute('aria-pressed', String(circleActive))

  dashedDetectBtn.classList.toggle('is-active', dashedActive)
  dashedDetectBtn.setAttribute('aria-pressed', String(dashedActive))

  cellDetectBtn.classList.toggle('is-active', cellActive)
  cellDetectBtn.setAttribute('aria-pressed', String(cellActive))

  pinCircleDetectBtn.classList.toggle('is-active', pinCircleActive)
  pinCircleDetectBtn.setAttribute('aria-pressed', String(pinCircleActive))
  pinArrowDetectBtn.classList.toggle('is-active', pinArrowActive)
  pinArrowDetectBtn.setAttribute('aria-pressed', String(pinArrowActive))
  wireMarkDetectBtn.classList.toggle('is-active', wireMarkActive)
  wireMarkDetectBtn.setAttribute('aria-pressed', String(wireMarkActive))
  symbolMatchDisplayBtn.classList.toggle('is-active', showSymbolMatches)
  symbolMatchDisplayBtn.setAttribute('aria-pressed', String(showSymbolMatches))
}

function toPageBBoxFromOverlayRect(
  left: number,
  top: number,
  right: number,
  bottom: number,
  pageHeight: number,
  scale: number,
): BBox {
  const x0 = left / scale
  const x1 = right / scale
  const y1 = pageHeight - top / scale
  const y0 = pageHeight - bottom / scale
  return { x0, y0, x1, y1, width: x1 - x0, height: y1 - y0 }
}

function normalizeApiBBox(box: Partial<BBox> & { x0: number; y0: number; x1: number; y1: number }): BBox {
  const x0 = Number(box.x0)
  const y0 = Number(box.y0)
  const x1 = Number(box.x1)
  const y1 = Number(box.y1)
  return { x0, y0, x1, y1, width: x1 - x0, height: y1 - y0 }
}

function mupdfBBoxToPdf(box: Partial<BBox> & { x0: number; y0: number; x1: number; y1: number }, pageHeight: number): BBox {
  const normalized = normalizeApiBBox(box)
  return normalizeApiBBox({
    x0: normalized.x0,
    y0: pageHeight - normalized.y1,
    x1: normalized.x1,
    y1: pageHeight - normalized.y0,
  })
}

function compactRegionToView(region: ApiVectorBoxGroup, pageHeight: number): VectorBoxGroup {
  const bboxMupdf = normalizeApiBBox(region.bbox)
  const vectorIndices = region.vectors
    .map((vector) => Number(vector.index))
    .filter((index) => Number.isFinite(index))
  return {
    bbox_pdf: mupdfBBoxToPdf(bboxMupdf, pageHeight),
    bbox_mupdf: bboxMupdf,
    vector_indices: vectorIndices,
    width: bboxMupdf.width,
    height: bboxMupdf.height,
    points: Array.isArray(region.points)
      ? region.points.map((point) => [Number(point[0]), Number(point[1])] as [number, number])
      : undefined,
    direction: region.direction,
  }
}

function bboxContains(outer: BBox, inner: BBox, epsilon = 0): boolean {
  return (
    inner.x0 >= outer.x0 - epsilon &&
    inner.y0 >= outer.y0 - epsilon &&
    inner.x1 <= outer.x1 + epsilon &&
    inner.y1 <= outer.y1 + epsilon
  )
}

function bboxIntersects(a: BBox, b: BBox, epsilon = 0): boolean {
  return !(a.x1 < b.x0 - epsilon || a.x0 > b.x1 + epsilon || a.y1 < b.y0 - epsilon || a.y0 > b.y1 + epsilon)
}

function extractShapeCode(shape: unknown): string | null {
  if (!shape || typeof shape !== 'object') {
    return null
  }
  const code = (shape as { code?: unknown }).code
  return typeof code === 'string' && code.trim() ? code.trim() : null
}

function formatPoint(point: unknown): string | null {
  if (!Array.isArray(point) || point.length < 2) {
    return null
  }
  const x = Number(point[0])
  const y = Number(point[1])
  if (!Number.isFinite(x) || !Number.isFinite(y)) {
    return null
  }
  return `${x.toFixed(3).replace(/\.?0+$/, '')} ${y.toFixed(3).replace(/\.?0+$/, '')}`
}

function shapeToRenderableCode(shape: unknown): string | null {
  if (!shape || typeof shape !== 'object') {
    return null
  }
  const record = shape as { type?: unknown; points?: unknown }
  const type = String(record.type ?? '')
  const points = Array.isArray(record.points) ? record.points : []

  if (type === 'line' && points.length >= 2) {
    const start = formatPoint(points[0])
    const end = formatPoint(points[1])
    return start && end ? `${start} m\n${end} l` : extractShapeCode(shape)
  }

  if (type === 'curve' && points.length >= 4) {
    const start = formatPoint(points[0])
    const p1 = formatPoint(points[1])
    const p2 = formatPoint(points[2])
    const p3 = formatPoint(points[3])
    return start && p1 && p2 && p3 ? `${start} m\n${p1} ${p2} ${p3} c` : extractShapeCode(shape)
  }

  if ((type === 'rect' || type === 'rectangle') && points.length >= 4) {
    const formatted = points.slice(0, 4).map(formatPoint)
    if (formatted.every(Boolean)) {
      return `${formatted[0]} m\n${formatted[1]} l\n${formatted[2]} l\n${formatted[3]} l\nh`
    }
  }

  return extractShapeCode(shape)
}

function buildSelectionSourceCode(shapes: unknown[], fallbackItems: VectorItem[]): string {
  const shapeCodes = shapes.map(shapeToRenderableCode).filter((code): code is string => Boolean(code))
  if (shapeCodes.length) {
    return [...new Set(shapeCodes)].join('\n')
  }
  return fallbackItems.map((item) => item.source.snippet.trim()).filter(Boolean).join('\n\n')
}

function findVectorItemsInsideArea(pageData: PageData, area: BBox): VectorItem[] {
  const vectors = pageData.items.filter((item): item is VectorItem => item.kind === 'vector_path')
  const contained = vectors.filter((item) => bboxContains(area, item.bbox, 0.25))
  if (contained.length) {
    return contained
  }
  return vectors.filter((item) => bboxIntersects(area, item.bbox, 0.25))
}

function loadPlaygroundCode(source: string): void {
  playgroundCode.value = source
  refreshPlayground(true)
}

function getMissingVectorRatio(): number {
  const percent = Number.parseFloat(missingVectorInput.value)
  if (!Number.isFinite(percent) || percent < 0 || percent >= 100) {
    missingVectorInput.value = '10'
    return 0.1
  }
  missingVectorInput.value = String(percent)
  return percent / 100
}

function getScaleRange(): [number, number] {
  const fallback: [number, number] = [0.5, 2.5]
  const minScale = Number.parseFloat(scaleMinInput.value)
  const maxScale = Number.parseFloat(scaleMaxInput.value)
  if (!Number.isFinite(minScale) || !Number.isFinite(maxScale) || minScale <= 0 || maxScale <= 0 || minScale > maxScale) {
    scaleMinInput.value = String(fallback[0])
    scaleMaxInput.value = String(fallback[1])
    return fallback
  }
  scaleMinInput.value = String(minScale)
  scaleMaxInput.value = String(maxScale)
  return [minScale, maxScale]
}

async function callVectorMatcher(
  mode: 'select' | 'match',
  bbox: BBox,
  options: {
    coordSpace?: 'pdf' | 'mupdf'
    searchScope?: 'current' | 'global'
    missingVectorRatio?: number
    scaleRange?: [number, number]
    targetPage?: number
    searchPage?: number
    targetShapes?: unknown[]
  } = {},
): Promise<{
  selected_shape_count: number
  selected_bbox_pdf: BBox | null
  selected_shapes: unknown[]
  searched_page_count?: number
  target_page?: number
  search_pages?: number[]
  matches?: VectorMatchResult[]
}> {
  if (!activeDocument) {
    throw new Error('No active PDF document.')
  }
  const response = await fetch('/api/vector-matcher', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode,
      pdf_url: activeDocument.pdf_url,
      page: options.targetPage ?? activePageNumber,
      search_page: options.searchPage ?? activePageNumber,
      bbox,
      target_shapes: options.targetShapes,
      coord_space: options.coordSpace ?? 'pdf',
      search_scope: options.searchScope ?? 'global',
      select_slack: mode === 'select' ? 0 : 0.1,
      missing_vector_ratio: options.missingVectorRatio ?? 0.1,
      scale_min: options.scaleRange?.[0] ?? 0.5,
      scale_max: options.scaleRange?.[1] ?? 2.5,
    }),
  })
  const payload = (await response.json()) as {
    ok: boolean
    error?: string
    result?: {
      selected_shape_count: number
      selected_bbox_pdf: BBox | null
      selected_shapes: unknown[]
      searched_page_count?: number
      target_page?: number
      search_pages?: number[]
      matches?: ApiVectorMatchResult[]
    }
  }
  if (!response.ok || !payload.ok || !payload.result) {
    throw new Error(payload.error || `Vector matcher request failed: ${response.status}`)
  }
  return {
    selected_shape_count: payload.result.selected_shape_count,
    selected_bbox_pdf: payload.result.selected_bbox_pdf ? normalizeApiBBox(payload.result.selected_bbox_pdf) : null,
    selected_shapes: payload.result.selected_shapes ?? [],
    searched_page_count: payload.result.searched_page_count,
    target_page: payload.result.target_page,
    search_pages: payload.result.search_pages,
    matches: (payload.result.matches ?? []).map((match) => ({
      ...match,
      page_number: Number(match.page_number),
      bbox_pdf: normalizeApiBBox(match.bbox_pdf),
      shape_count: Number(match.shape_count ?? match.vectors?.length ?? 0),
    })),
  }
}

async function callVectorEntityDetection(): Promise<VectorEntityResult> {
  if (!activeDocument) {
    throw new Error('No active PDF document.')
  }
  const response = await fetch('/api/vector-matcher', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: 'entities',
      pdf_url: activeDocument.pdf_url,
      page: activePageNumber,
    }),
  })
  const payload = (await response.json()) as {
    ok: boolean
    error?: string
    result?: {
      category: string
      entities: ApiVectorEntityGroup[]
    }
  }
  if (!response.ok || !payload.ok || !payload.result) {
    throw new Error(payload.error || `Vector entity request failed: ${response.status}`)
  }
  const pageHeight = currentPageState?.pageData.page_size.height_pt
  if (!pageHeight) {
    throw new Error('Page height is unavailable for vector entity conversion.')
  }
  const groups = (payload.result.entities ?? []).map((group, groupIndex) => {
    const indices = group.vectors
      .map((vector) => Number(vector.index))
      .filter((index) => Number.isFinite(index))
    const bboxMupdf = normalizeApiBBox(group.bbox)
    return {
      root: indices[0] ?? groupIndex,
      indices,
      vectors: group.vectors,
      bbox_pdf: mupdfBBoxToPdf(bboxMupdf, pageHeight),
      bbox_mupdf: bboxMupdf,
      size: group.vectors.length,
    }
  })
  return {
    page_number: activePageNumber,
    entity_count: groups.length,
    groups,
  }
}

async function callSplitPageDetection(): Promise<SplitPageResult> {
  if (!activeDocument) {
    throw new Error('No active PDF document.')
  }
  const response = await fetch('/api/vector-matcher', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: 'split_page',
      pdf_url: activeDocument.pdf_url,
      page: activePageNumber,
    }),
  })
  const payload = (await response.json()) as {
    ok: boolean
    error?: string
    result?: {
      page_number: number
      page_bbox: BBox
      content_bbox: BBox
      info_bbox: BBox | null
    }
  }
  if (!response.ok || !payload.ok || !payload.result) {
    throw new Error(payload.error || `Split page request failed: ${response.status}`)
  }
  const pageHeight = currentPageState?.pageData.page_size.height_pt
  if (!pageHeight) {
    throw new Error('Page height is unavailable for split page conversion.')
  }
  return {
    page_number: Number(payload.result.page_number),
    page_bbox: mupdfBBoxToPdf(normalizeApiBBox(payload.result.page_bbox), pageHeight),
    content_bbox: mupdfBBoxToPdf(normalizeApiBBox(payload.result.content_bbox), pageHeight),
    info_bbox: payload.result.info_bbox ? mupdfBBoxToPdf(normalizeApiBBox(payload.result.info_bbox), pageHeight) : null,
  }
}

async function callVectorBoxDetection(group: VectorEntityGroup, entityIndex: number): Promise<VectorBoxResult> {
  if (!activeDocument) {
    throw new Error('No active PDF document.')
  }
  const response = await fetch('/api/vector-matcher', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: 'boxes',
      pdf_url: activeDocument.pdf_url,
      page: activePageNumber,
      entity_root: group.root,
      entity_indices: group.indices,
      entity_vectors: group.vectors,
    }),
  })
  const payload = (await response.json()) as {
    ok: boolean
    error?: string
    result?: {
      category: string
      regions: ApiVectorBoxGroup[]
    }
  }
  if (!response.ok || !payload.ok || !payload.result) {
    throw new Error(payload.error || `Vector box request failed: ${response.status}`)
  }
  const pageHeight = currentPageState?.pageData.page_size.height_pt
  if (!pageHeight) {
    throw new Error('Page height is unavailable for vector box conversion.')
  }
  const boxes = (payload.result.regions ?? []).map((region) => compactRegionToView(region, pageHeight))
  return {
    page_number: activePageNumber,
    entity_root: group.root,
    entity_index: entityIndex,
    box_count: boxes.length,
    boxes,
  }
}

async function callVectorCircleDetection(group: VectorEntityGroup, entityIndex: number): Promise<VectorCircleResult> {
  if (!activeDocument) {
    throw new Error('No active PDF document.')
  }
  const response = await fetch('/api/vector-matcher', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: 'circles',
      pdf_url: activeDocument.pdf_url,
      page: activePageNumber,
      entity_root: group.root,
      entity_indices: group.indices,
      entity_vectors: group.vectors,
    }),
  })
  const payload = (await response.json()) as {
    ok: boolean
    error?: string
    result?: {
      category: string
      regions: ApiVectorBoxGroup[]
    }
  }
  if (!response.ok || !payload.ok || !payload.result) {
    throw new Error(payload.error || `Vector circle request failed: ${response.status}`)
  }
  const pageHeight = currentPageState?.pageData.page_size.height_pt
  if (!pageHeight) {
    throw new Error('Page height is unavailable for vector circle conversion.')
  }
  const circles = (payload.result.regions ?? []).map((region) => compactRegionToView(region, pageHeight))
  return {
    page_number: activePageNumber,
    entity_root: group.root,
    entity_index: entityIndex,
    circle_count: circles.length,
    circles,
  }
}

async function callVectorDashedDetection(group: VectorEntityGroup, entityIndex: number): Promise<VectorDashedResult> {
  if (!activeDocument) {
    throw new Error('No active PDF document.')
  }
  const response = await fetch('/api/vector-matcher', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: 'dashed',
      pdf_url: activeDocument.pdf_url,
      page: activePageNumber,
      entity_root: group.root,
      entity_indices: group.indices,
      entity_vectors: group.vectors,
    }),
  })
  const payload = (await response.json()) as {
    ok: boolean
    error?: string
    result?: {
      category: string
      regions: ApiVectorBoxGroup[]
    }
  }
  if (!response.ok || !payload.ok || !payload.result) {
    throw new Error(payload.error || `Vector dashed request failed: ${response.status}`)
  }
  const pageHeight = currentPageState?.pageData.page_size.height_pt
  if (!pageHeight) {
    throw new Error('Page height is unavailable for dashed region conversion.')
  }
  const dashed = (payload.result.regions ?? []).map((region) => compactRegionToView(region, pageHeight))
  return {
    page_number: activePageNumber,
    entity_root: group.root,
    entity_index: entityIndex,
    dashed_count: dashed.length,
    dashed,
  }
}

async function callVectorCellDetection(group: VectorEntityGroup, entityIndex: number): Promise<VectorCellResult> {
  if (!activeDocument) {
    throw new Error('No active PDF document.')
  }
  const response = await fetch('/api/vector-matcher', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: 'cells',
      pdf_url: activeDocument.pdf_url,
      page: activePageNumber,
      entity_root: group.root,
      entity_indices: group.indices,
      entity_vectors: group.vectors,
    }),
  })
  const payload = (await response.json()) as {
    ok: boolean
    error?: string
    result?: {
      category: string
      regions: ApiVectorBoxGroup[]
    }
  }
  if (!response.ok || !payload.ok || !payload.result) {
    throw new Error(payload.error || `Vector cell request failed: ${response.status}`)
  }
  const pageHeight = currentPageState?.pageData.page_size.height_pt
  if (!pageHeight) {
    throw new Error('Page height is unavailable for cell conversion.')
  }
  const cells = (payload.result.regions ?? []).map((region) => compactRegionToView(region, pageHeight))
  return {
    page_number: activePageNumber,
    entity_root: group.root,
    entity_index: entityIndex,
    cell_count: cells.length,
    cells,
  }
}

async function callVectorPinDetection(
  kind: 'circle' | 'arrow' | 'wire_mark',
  group: VectorEntityGroup,
  entityIndex: number,
): Promise<VectorPinResult> {
  if (!activeDocument) throw new Error('No active PDF document.')
  const response = await fetch('/api/vector-matcher', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: kind === 'circle' ? 'pin_circles' : kind === 'arrow' ? 'pin_arrows' : 'pin_wire_marks',
      pdf_url: activeDocument.pdf_url,
      page: activePageNumber,
      entity_root: group.root,
      entity_indices: group.indices,
      entity_vectors: group.vectors,
    }),
  })
  const payload = (await response.json()) as {
    ok: boolean
    error?: string
    result?: { regions: ApiVectorBoxGroup[] }
  }
  if (!response.ok || !payload.ok || !payload.result) {
    throw new Error(payload.error || `Vector pin request failed: ${response.status}`)
  }
  const pageHeight = currentPageState?.pageData.page_size.height_pt
  if (!pageHeight) throw new Error('Page height is unavailable for pin conversion.')
  const pins = (payload.result.regions ?? []).map((region) => compactRegionToView(region, pageHeight))
  return {
    page_number: activePageNumber,
    entity_root: group.root,
    entity_index: entityIndex,
    kind,
    pin_count: pins.length,
    pins,
  }
}

function createMatchOverlayBox(bbox: BBox, index: number, scale: number, pageHeight: number): HTMLButtonElement {
  const node = document.createElement('button')
  node.type = 'button'
  node.className = 'vector-match-box'
  node.dataset.matchIndex = String(index)
  node.title = `Vector match ${index + 1}`
  node.style.left = `${bbox.x0 * scale}px`
  node.style.top = `${(pageHeight - bbox.y1) * scale}px`
  node.style.width = `${Math.max(bbox.width * scale, 6)}px`
  node.style.height = `${Math.max(bbox.height * scale, 6)}px`
  node.addEventListener('click', (event) => {
    event.preventDefault()
    event.stopPropagation()
    scrollToBBoxCenter(bbox)
  })
  return node
}

function createEntityOverlayBox(group: VectorEntityGroup, index: number, scale: number, pageHeight: number): HTMLButtonElement {
  const bbox = group.bbox_pdf
  const node = document.createElement('button')
  node.type = 'button'
  node.className = 'vector-entity-box'
  node.classList.toggle('is-active', activeVectorEntityIndex === index)
  node.dataset.entityIndex = String(index)
  node.title = `Entity ${index + 1}: ${group.size} vector${group.size === 1 ? '' : 's'}`
  node.style.left = `${bbox.x0 * scale}px`
  node.style.top = `${(pageHeight - bbox.y1) * scale}px`
  node.style.width = `${Math.max(bbox.width * scale, 6)}px`
  node.style.height = `${Math.max(bbox.height * scale, 6)}px`
  for (const edge of ['top', 'right', 'bottom', 'left']) {
    const edgeNode = document.createElement('span')
    edgeNode.className = `vector-box-edge vector-box-edge-${edge}`
    node.appendChild(edgeNode)
  }
  node.addEventListener('click', (event) => {
    event.preventDefault()
    event.stopPropagation()
    if (activeVectorEntityIndex === index) {
      activeVectorEntityIndex = null
      clearVectorBoxResult()
      clearVectorCircleResult()
      clearVectorDashedResult()
      clearVectorCellResult()
      clearVectorPinResult()
    } else {
      activeVectorEntityIndex = index
      clearVectorBoxResult()
      clearVectorCircleResult()
      clearVectorDashedResult()
      clearVectorCellResult()
      clearVectorPinResult()
    }
    updateVectorEntityActionButtons()
    renderVectorEntityOverlays()
    scrollToBBoxCenter(bbox)
  })
  return node
}

function createBoxOverlayBox(group: VectorBoxGroup, index: number, scale: number, pageHeight: number): HTMLButtonElement {
  const bbox = group.bbox_pdf
  const node = document.createElement('button')
  node.type = 'button'
  node.className = 'vector-box-detect-box'
  node.classList.toggle('is-active', activeVectorBoxIndex === index)
  node.dataset.boxIndex = String(index)
  node.title = `Box ${index + 1}: ${group.vector_indices.length} boundary vector${group.vector_indices.length === 1 ? '' : 's'}`
  node.style.left = `${bbox.x0 * scale}px`
  node.style.top = `${(pageHeight - bbox.y1) * scale}px`
  node.style.width = `${Math.max(bbox.width * scale, 6)}px`
  node.style.height = `${Math.max(bbox.height * scale, 6)}px`
  node.addEventListener('click', (event) => {
    event.preventDefault()
    event.stopPropagation()
    activeVectorBoxIndex = activeVectorBoxIndex === index ? null : index
    renderVectorBoxOverlays()
    scrollToBBoxCenter(bbox)
  })
  return node
}

function createCircleOverlayBox(group: VectorBoxGroup, index: number, scale: number, pageHeight: number): HTMLButtonElement {
  const bbox = group.bbox_pdf
  const node = document.createElement('button')
  node.type = 'button'
  node.className = 'vector-circle-detect-box'
  node.classList.toggle('is-active', activeVectorCircleIndex === index)
  node.dataset.circleIndex = String(index)
  node.title = `Circle ${index + 1}: ${group.vector_indices.length} boundary vector${group.vector_indices.length === 1 ? '' : 's'}`
  node.style.left = `${bbox.x0 * scale}px`
  node.style.top = `${(pageHeight - bbox.y1) * scale}px`
  node.style.width = `${Math.max(bbox.width * scale, 6)}px`
  node.style.height = `${Math.max(bbox.height * scale, 6)}px`
  node.addEventListener('click', (event) => {
    event.preventDefault()
    event.stopPropagation()
    activeVectorCircleIndex = activeVectorCircleIndex === index ? null : index
    renderVectorCircleOverlays()
    scrollToBBoxCenter(bbox)
  })
  return node
}

const SVG_NS = 'http://www.w3.org/2000/svg'

function createDashedOverlayBox(
  group: VectorBoxGroup,
  index: number,
  scale: number,
  pageHeight: number,
): SVGSVGElement | HTMLButtonElement {
  const title = `Dashed polygon ${index + 1}: ${group.vector_indices.length} boundary vector${group.vector_indices.length === 1 ? '' : 's'}`

  // The dashed shape is usually a real polygon, so trace its outline rather than
  // a bounding box. Fall back to a bbox rectangle if no polygon is available.
  const points = group.points
  if (!points || points.length < 3) {
    const bbox = group.bbox_pdf
    const node = document.createElement('button')
    node.type = 'button'
    node.className = 'vector-dashed-detect-box'
    node.classList.toggle('is-active', activeVectorDashedIndex === index)
    node.dataset.dashedIndex = String(index)
    node.title = title
    node.style.left = `${bbox.x0 * scale}px`
    node.style.top = `${(pageHeight - bbox.y1) * scale}px`
    node.style.width = `${Math.max(bbox.width * scale, 6)}px`
    node.style.height = `${Math.max(bbox.height * scale, 6)}px`
    node.addEventListener('click', (event) => {
      event.preventDefault()
      event.stopPropagation()
      activeVectorDashedIndex = activeVectorDashedIndex === index ? null : index
      renderVectorDashedOverlays()
      scrollToBBoxCenter(bbox)
    })
    return node
  }

  // Points are in PyMuPDF (top-left origin) coordinates: screen = point * scale.
  const screen = points.map(([x, y]) => [x * scale, y * scale] as [number, number])
  const xs = screen.map((point) => point[0])
  const ys = screen.map((point) => point[1])
  const minX = Math.min(...xs)
  const minY = Math.min(...ys)
  const maxX = Math.max(...xs)
  const maxY = Math.max(...ys)
  const pad = 3

  const svg = document.createElementNS(SVG_NS, 'svg')
  svg.setAttribute('class', 'vector-dashed-detect-box')
  svg.classList.toggle('is-active', activeVectorDashedIndex === index)
  svg.dataset.dashedIndex = String(index)
  svg.style.position = 'absolute'
  svg.style.left = `${minX - pad}px`
  svg.style.top = `${minY - pad}px`
  svg.style.width = `${maxX - minX + pad * 2}px`
  svg.style.height = `${maxY - minY + pad * 2}px`
  svg.style.overflow = 'visible'
  svg.style.pointerEvents = 'none'

  const polygon = document.createElementNS(SVG_NS, 'polygon')
  polygon.setAttribute('class', 'vector-dashed-detect-shape')
  polygon.setAttribute(
    'points',
    screen.map((point) => `${point[0] - minX + pad},${point[1] - minY + pad}`).join(' '),
  )
  svg.appendChild(polygon)

  const titleNode = document.createElementNS(SVG_NS, 'title')
  titleNode.textContent = title
  svg.appendChild(titleNode)

  svg.addEventListener('click', (event) => {
    event.preventDefault()
    event.stopPropagation()
    activeVectorDashedIndex = activeVectorDashedIndex === index ? null : index
    renderVectorDashedOverlays()
    scrollToBBoxCenter(group.bbox_pdf)
  })
  return svg
}

function createCellOverlay(
  group: VectorBoxGroup,
  index: number,
  scale: number,
  pageHeight: number,
): SVGSVGElement | HTMLButtonElement {
  const title = `Cell ${index + 1}: ${group.vector_indices.length} boundary vector${group.vector_indices.length === 1 ? '' : 's'}`
  const points = group.points
  if (!points || points.length < 3) {
    const bbox = group.bbox_pdf
    const node = document.createElement('button')
    node.type = 'button'
    node.className = 'vector-cell-detect-box'
    node.classList.toggle('is-active', activeVectorCellIndex === index)
    node.dataset.cellIndex = String(index)
    node.title = title
    node.style.left = `${bbox.x0 * scale}px`
    node.style.top = `${(pageHeight - bbox.y1) * scale}px`
    node.style.width = `${Math.max(bbox.width * scale, 6)}px`
    node.style.height = `${Math.max(bbox.height * scale, 6)}px`
    node.addEventListener('click', (event) => {
      event.preventDefault()
      event.stopPropagation()
      activeVectorCellIndex = activeVectorCellIndex === index ? null : index
      renderVectorCellOverlays()
      scrollToBBoxCenter(bbox)
    })
    return node
  }

  const screen = points.map(([x, y]) => [x * scale, y * scale] as [number, number])
  const xs = screen.map((point) => point[0])
  const ys = screen.map((point) => point[1])
  const minX = Math.min(...xs)
  const minY = Math.min(...ys)
  const maxX = Math.max(...xs)
  const maxY = Math.max(...ys)
  const pad = 3

  const svg = document.createElementNS(SVG_NS, 'svg')
  svg.setAttribute('class', 'vector-cell-detect-box')
  svg.classList.toggle('is-active', activeVectorCellIndex === index)
  svg.dataset.cellIndex = String(index)
  svg.style.position = 'absolute'
  svg.style.left = `${minX - pad}px`
  svg.style.top = `${minY - pad}px`
  svg.style.width = `${maxX - minX + pad * 2}px`
  svg.style.height = `${maxY - minY + pad * 2}px`
  svg.style.overflow = 'visible'
  svg.style.pointerEvents = 'none'

  const polygon = document.createElementNS(SVG_NS, 'polygon')
  polygon.setAttribute('class', 'vector-cell-detect-shape')
  polygon.setAttribute(
    'points',
    screen.map((point) => `${point[0] - minX + pad},${point[1] - minY + pad}`).join(' '),
  )
  svg.appendChild(polygon)

  const titleNode = document.createElementNS(SVG_NS, 'title')
  titleNode.textContent = title
  svg.appendChild(titleNode)

  svg.addEventListener('click', (event) => {
    event.preventDefault()
    event.stopPropagation()
    activeVectorCellIndex = activeVectorCellIndex === index ? null : index
    renderVectorCellOverlays()
    scrollToBBoxCenter(group.bbox_pdf)
  })
  return svg
}

function createPinOverlayBox(
  group: VectorBoxGroup,
  index: number,
  kind: 'circle' | 'arrow' | 'wire_mark',
  scale: number,
  pageHeight: number,
): HTMLButtonElement {
  const bbox = group.bbox_pdf
  const node = document.createElement('button')
  node.type = 'button'
  node.className = `vector-pin-detect-box vector-pin-${kind}-detect-box`
  const label = kind === 'circle' ? 'Circle pin' : kind === 'arrow' ? 'Arrow pin' : 'Wire mark'
  node.title = `${label} ${index + 1}${group.direction ? ` (${group.direction})` : ''}`
  node.style.left = `${bbox.x0 * scale - 3}px`
  node.style.top = `${(pageHeight - bbox.y1) * scale - 3}px`
  node.style.width = `${Math.max(bbox.width * scale + 6, 10)}px`
  node.style.height = `${Math.max(bbox.height * scale + 6, 10)}px`
  node.addEventListener('click', (event) => {
    event.preventDefault()
    event.stopPropagation()
    scrollToBBoxCenter(bbox)
  })
  return node
}

function createSplitPageOverlayBox(
  bbox: BBox,
  kind: 'content' | 'info',
  scale: number,
  pageHeight: number,
): HTMLDivElement {
  const node = document.createElement('div')
  node.className = `split-page-box split-page-box-${kind}`
  node.title = kind === 'content' ? 'Drawing/content box' : 'Information box'
  node.style.left = `${bbox.x0 * scale}px`
  node.style.top = `${(pageHeight - bbox.y1) * scale}px`
  node.style.width = `${Math.max(bbox.width * scale, 6)}px`
  node.style.height = `${Math.max(bbox.height * scale, 6)}px`
  return node
}

function getVectorMatchPageGroups(): Array<{ pageNumber: number; matches: VectorMatchResult[] }> {
  const grouped = new Map<number, VectorMatchResult[]>()
  for (const match of vectorMatchResults) {
    if (!Number.isFinite(match.page_number)) {
      continue
    }
    const pageMatches = grouped.get(match.page_number) ?? []
    pageMatches.push(match)
    grouped.set(match.page_number, pageMatches)
  }
  return [...grouped.entries()]
    .sort(([a], [b]) => a - b)
    .map(([pageNumber, matches]) => ({ pageNumber, matches }))
}

function setVectorMatchMenuSearching(totalPages: number): void {
  vectorMatchResultsLabel = `Searching ${totalPages} page${totalPages === 1 ? '' : 's'}...`
  renderVectorMatchResultsSelect()
}

function renderVectorMatchResultsSelect(): void {
  const groups = getVectorMatchPageGroups()
  const total = vectorMatchResults.length
  const label = total
    ? `Results: ${total} match${total === 1 ? '' : 'es'}`
    : vectorMatchResultsLabel
  vectorMatchResultsSelect.innerHTML = [
    `<option value="">${escapeHtml(label)}</option>`,
    ...groups.map(
      ({ pageNumber, matches }) =>
        `<option value="${pageNumber}">Page ${pageNumber} (${matches.length})</option>`,
    ),
  ].join('')
  vectorMatchResultsSelect.value = ''
  vectorMatchResultsSelect.disabled = !total
}

function renderSplitPageOverlays(): void {
  const st = currentPageState
  if (!st) {
    return
  }
  st.overlay.querySelectorAll('.split-page-box').forEach((node) => node.remove())
  if (!splitPageResult || splitPageResult.page_number !== st.pageNumber) {
    updateDetectButtons()
    return
  }
  st.overlay.appendChild(
    createSplitPageOverlayBox(splitPageResult.content_bbox, 'content', st.scale, st.pageData.page_size.height_pt),
  )
  if (splitPageResult.info_bbox) {
    st.overlay.appendChild(
      createSplitPageOverlayBox(splitPageResult.info_bbox, 'info', st.scale, st.pageData.page_size.height_pt),
    )
  }
  updateDetectButtons()
}

function renderVectorMatchOverlays(): void {
  const st = currentPageState
  if (!st) {
    return
  }
  st.overlay.querySelectorAll('.vector-match-box').forEach((node) => node.remove())
  const pageMatches = vectorMatchResults.filter((match) => match.page_number === st.pageNumber)
  for (let index = 0; index < pageMatches.length; index += 1) {
    st.overlay.appendChild(createMatchOverlayBox(pageMatches[index].bbox_pdf, index, st.scale, st.pageData.page_size.height_pt))
  }
  renderSymbolMatchOverlays()
}

function renderVectorEntityOverlays(): void {
  const st = currentPageState
  if (!st) {
    return
  }
  st.overlay.querySelectorAll('.vector-entity-box, .vector-content-bbox').forEach((node) => node.remove())
  if (!vectorEntityResult || vectorEntityResult.page_number !== st.pageNumber) {
    activeVectorEntityIndex = null
    clearVectorBoxResult()
    clearVectorCircleResult()
    clearVectorDashedResult()
    clearVectorCellResult()
    updateVectorEntityActionButtons()
    updateDetectButtons()
    return
  }
  for (let index = 0; index < vectorEntityResult.groups.length; index += 1) {
    st.overlay.appendChild(
      createEntityOverlayBox(vectorEntityResult.groups[index], index, st.scale, st.pageData.page_size.height_pt),
    )
  }
  updateVectorEntityActionButtons()
  updateDetectButtons()
}

function renderVectorBoxOverlays(): void {
  const st = currentPageState
  if (!st) {
    return
  }
  st.overlay.querySelectorAll('.vector-box-detect-box').forEach((node) => node.remove())
  if (!vectorBoxResult || vectorBoxResult.page_number !== st.pageNumber) {
    updateDetectButtons()
    return
  }
  const selectedGroup = getSelectedVectorEntityGroup()
  if (!selectedGroup || vectorBoxResult.entity_root !== selectedGroup.root) {
    updateDetectButtons()
    return
  }
  for (let index = 0; index < vectorBoxResult.boxes.length; index += 1) {
    st.overlay.appendChild(
      createBoxOverlayBox(vectorBoxResult.boxes[index], index, st.scale, st.pageData.page_size.height_pt),
    )
  }
  updateDetectButtons()
}

function renderVectorCircleOverlays(): void {
  const st = currentPageState
  if (!st) {
    return
  }
  st.overlay.querySelectorAll('.vector-circle-detect-box').forEach((node) => node.remove())
  if (!vectorCircleResult || vectorCircleResult.page_number !== st.pageNumber) {
    updateDetectButtons()
    return
  }
  const selectedGroup = getSelectedVectorEntityGroup()
  if (!selectedGroup || vectorCircleResult.entity_root !== selectedGroup.root) {
    updateDetectButtons()
    return
  }
  for (let index = 0; index < vectorCircleResult.circles.length; index += 1) {
    st.overlay.appendChild(
      createCircleOverlayBox(vectorCircleResult.circles[index], index, st.scale, st.pageData.page_size.height_pt),
    )
  }
  updateDetectButtons()
}

function renderVectorDashedOverlays(): void {
  const st = currentPageState
  if (!st) {
    return
  }
  st.overlay.querySelectorAll('.vector-dashed-detect-box').forEach((node) => node.remove())
  if (!vectorDashedResult || vectorDashedResult.page_number !== st.pageNumber) {
    updateDetectButtons()
    return
  }
  const selectedGroup = getSelectedVectorEntityGroup()
  if (!selectedGroup || vectorDashedResult.entity_root !== selectedGroup.root) {
    updateDetectButtons()
    return
  }
  for (let index = 0; index < vectorDashedResult.dashed.length; index += 1) {
    st.overlay.appendChild(
      createDashedOverlayBox(vectorDashedResult.dashed[index], index, st.scale, st.pageData.page_size.height_pt),
    )
  }
  updateDetectButtons()
}

function renderVectorCellOverlays(): void {
  const st = currentPageState
  if (!st) {
    return
  }
  st.overlay.querySelectorAll('.vector-cell-detect-box').forEach((node) => node.remove())
  if (!vectorCellResult || vectorCellResult.page_number !== st.pageNumber) {
    updateDetectButtons()
    return
  }
  const selectedGroup = getSelectedVectorEntityGroup()
  if (!selectedGroup || vectorCellResult.entity_root !== selectedGroup.root) {
    updateDetectButtons()
    return
  }
  for (let index = 0; index < vectorCellResult.cells.length; index += 1) {
    st.overlay.appendChild(
      createCellOverlay(vectorCellResult.cells[index], index, st.scale, st.pageData.page_size.height_pt),
    )
  }
  updateDetectButtons()
}

function renderVectorPinOverlays(): void {
  const st = currentPageState
  if (!st) return
  st.overlay.querySelectorAll('.vector-pin-detect-box').forEach((node) => node.remove())
  const selectedGroup = getSelectedVectorEntityGroup()
  if (!selectedGroup) {
    updateDetectButtons()
    return
  }
  for (const result of [vectorPinCircleResult, vectorPinArrowResult, vectorWireMarkResult]) {
    if (!result || result.page_number !== st.pageNumber || result.entity_root !== selectedGroup.root) continue
    for (let index = 0; index < result.pins.length; index += 1) {
      st.overlay.appendChild(
        createPinOverlayBox(result.pins[index], index, result.kind, st.scale, st.pageData.page_size.height_pt),
      )
    }
  }
  updateDetectButtons()
}

function setAreaSelectMode(enabled: boolean): void {
  isAreaSelectMode = enabled
  areaSelectToggleBtn.setAttribute('aria-pressed', String(enabled))
  areaSelectToggleBtn.classList.toggle('is-active', enabled)
  if (!enabled && areaSelectionBox) {
    areaSelectionBox.remove()
    areaSelectionBox = null
  }
  const overlay = currentPageState?.overlay
  if (overlay) {
    overlay.classList.toggle('area-select-mode', enabled)
  }
}

async function selectVectorsInsideArea(pageData: PageData, area: BBox): Promise<void> {
  try {
    setStatus('Extracting vector shapes from selected area...')
    const result = await callVectorMatcher('select', area)
    if (!result.selected_shape_count || !result.selected_bbox_pdf) {
      setStatus('No vector shape was fully enclosed by the selected area.')
      setVectorMatcherSelection(null)
      return
    }

    const selectedItems = findVectorItemsInsideArea(pageData, area)
    const selectedSource = buildSelectionSourceCode(result.selected_shapes, selectedItems)
    setStatus(`Selected ${result.selected_shape_count} vector shapes. Use Stash Shape in the playground toolbar when ready.`)
    setVectorMatcherSelection({
      pageNumber: pageData.page_number,
      queryBBox: area,
      bbox: result.selected_bbox_pdf,
      shapeCount: result.selected_shape_count,
      shapes: result.selected_shapes,
      sourceCode: selectedSource,
      itemIds: selectedItems.map((item) => item.id),
    })
  } catch (error) {
    setStatus(`Vector selection failed: ${error instanceof Error ? error.message : String(error)}`)
    setVectorMatcherSelection(null)
  }
}

function attachAreaSelectionHandlers(overlay: HTMLDivElement, pageData: PageData, scale: number): void {
  overlay.classList.toggle('area-select-mode', isAreaSelectMode)

  const clearDragState = () => {
    areaDragStart = null
    areaDragPointerId = null
  }

  const finishAreaSelection = (endX: number, endY: number) => {
    if (!areaDragStart) {
      clearDragState()
      return
    }
    const left = Math.min(areaDragStart.x, endX)
    const right = Math.max(areaDragStart.x, endX)
    const top = Math.min(areaDragStart.y, endY)
    const bottom = Math.max(areaDragStart.y, endY)

    if (areaSelectionBox) {
      areaSelectionBox.remove()
      areaSelectionBox = null
    }

    clearDragState()
    if (right - left < 4 || bottom - top < 4) {
      return
    }

    const pageBBox = toPageBBoxFromOverlayRect(left, top, right, bottom, pageData.page_size.height_pt, scale)
    void selectVectorsInsideArea(pageData, pageBBox)
  }

  overlay.addEventListener('pointerdown', (event) => {
    if (!isAreaSelectMode || event.button !== 0) {
      return
    }
    event.preventDefault()
    event.stopPropagation()
    const rect = overlay.getBoundingClientRect()
    const x = clamp(event.clientX - rect.left, 0, rect.width)
    const y = clamp(event.clientY - rect.top, 0, rect.height)
    areaDragStart = { x, y }
    areaDragPointerId = event.pointerId
    overlay.setPointerCapture(event.pointerId)
    areaSelectionBox = document.createElement('div')
    areaSelectionBox.className = 'area-selection-rect'
    areaSelectionBox.style.left = `${x}px`
    areaSelectionBox.style.top = `${y}px`
    areaSelectionBox.style.width = '0px'
    areaSelectionBox.style.height = '0px'
    overlay.appendChild(areaSelectionBox)
  })

  overlay.addEventListener('pointermove', (event) => {
    if (!isAreaSelectMode || areaDragStart === null || areaDragPointerId !== event.pointerId || !areaSelectionBox) {
      return
    }
    event.preventDefault()
    const rect = overlay.getBoundingClientRect()
    const x = clamp(event.clientX - rect.left, 0, rect.width)
    const y = clamp(event.clientY - rect.top, 0, rect.height)
    const left = Math.min(areaDragStart.x, x)
    const right = Math.max(areaDragStart.x, x)
    const top = Math.min(areaDragStart.y, y)
    const bottom = Math.max(areaDragStart.y, y)
    areaSelectionBox.style.left = `${left}px`
    areaSelectionBox.style.top = `${top}px`
    areaSelectionBox.style.width = `${right - left}px`
    areaSelectionBox.style.height = `${bottom - top}px`
  })

  overlay.addEventListener('pointerup', (event) => {
    if (!isAreaSelectMode || areaDragPointerId !== event.pointerId) {
      return
    }
    event.preventDefault()
    const rect = overlay.getBoundingClientRect()
    const x = clamp(event.clientX - rect.left, 0, rect.width)
    const y = clamp(event.clientY - rect.top, 0, rect.height)
    finishAreaSelection(x, y)
  })

  overlay.addEventListener('pointercancel', (event) => {
    if (areaDragPointerId !== event.pointerId) {
      return
    }
    if (areaSelectionBox) {
      areaSelectionBox.remove()
      areaSelectionBox = null
    }
    clearDragState()
  })
}

function itemArea(item: ReaderItem): number {
  return Math.max(item.bbox.width * item.bbox.height, 1)
}

function compareHitPriority(a: ReaderItem, b: ReaderItem): number {
  const kindWeight: Record<ReaderItem['kind'], number> = {
    link: 4,
    text: 3,
    image: 2,
    vector_path: 1,
  }
  const kindDiff = kindWeight[b.kind] - kindWeight[a.kind]
  if (kindDiff !== 0) {
    return kindDiff
  }

  const areaDiff = itemArea(a) - itemArea(b)
  if (areaDiff !== 0) {
    return areaDiff
  }

  return a.id.localeCompare(b.id)
}

function getRenderedBounds(item: ReaderItem, scale: number, pageHeight: number) {
  const left = item.bbox.x0 * scale
  const top = (pageHeight - item.bbox.y1) * scale
  const minSize = item.kind === 'link' ? 8 : item.kind === 'text' || item.kind === 'image' ? 6 : 4
  const width = Math.max(item.bbox.width * scale, minSize)
  const height = Math.max(item.bbox.height * scale, minSize)

  return { left, top, width, height }
}

function getHitCandidates(pageData: PageData, hitX: number, hitY: number, scale: number): ReaderItem[] {
  return pageData.items
    .filter((item) => isLayerVisible(item.kind))
    .filter((item) => {
      const bounds = getRenderedBounds(item, scale, pageData.page_size.height_pt)
      return (
        hitX >= bounds.left &&
        hitX <= bounds.left + bounds.width &&
        hitY >= bounds.top &&
        hitY <= bounds.top + bounds.height
      )
    })
    .sort(compareHitPriority)
}

function selectItemAtPoint(pageData: PageData, hitX: number, hitY: number, scale: number): void {
  if (isInfoTraceActive()) {
    setStatus('Info Trace is active. Stop tracing before using normal PDF selection highlights.')
    renderInfoTraceHighlights()
    return
  }
  const candidates = getHitCandidates(pageData, hitX, hitY, scale)
  if (!candidates.length) {
    setSelection(null, null)
    return
  }

  let nextItem = candidates[0]
  const currentIndex = candidates.findIndex((item) => item.id === activeSelectionId)
  if (currentIndex >= 0) {
    nextItem = candidates[(currentIndex + 1) % candidates.length]
  }

  setSelection(nextItem, pageData)
}

function hyperlinkTargetPage(item: LinkItem): number | null {
  const value = item.link.target?.page
  if (typeof value === 'number' && Number.isInteger(value)) {
    return value + 1
  }
  if (typeof value === 'string' && /^\d+$/.test(value.trim())) {
    return Number.parseInt(value, 10)
  }
  return null
}

function targetRegionBounds(
  region: HyperlinkTargetHighlight['region'],
  pageWidth: number,
  pageHeight: number,
): { x0: number; y0: number; x1: number; y1: number; topLeft: boolean } | null {
  if (region.type === 'whole_page') {
    return { x0: 0, y0: 0, x1: pageWidth, y1: pageHeight, topLeft: true }
  }
  const nested = region.bbox
  if (
    nested &&
    [nested.x0, nested.y0, nested.x1, nested.y1].every((value) => typeof value === 'number')
  ) {
    return {
      x0: nested.x0 as number,
      y0: nested.y0 as number,
      x1: nested.x1 as number,
      y1: nested.y1 as number,
      topLeft: region.coordinate_space?.includes('top-left') ?? true,
    }
  }
  if ([region.x0, region.y0, region.x1, region.y1].every((value) => typeof value === 'number')) {
    return {
      x0: region.x0 as number,
      y0: region.y0 as number,
      x1: region.x1 as number,
      y1: region.y1 as number,
      topLeft: region.coordinate_space?.includes('top-left') ?? false,
    }
  }
  if ([region.x, region.y, region.width, region.height].every((value) => typeof value === 'number')) {
    return {
      x0: region.x as number,
      y0: region.y as number,
      x1: (region.x as number) + (region.width as number),
      y1: (region.y as number) + (region.height as number),
      topLeft: true,
    }
  }
  return null
}

function renderHyperlinkTargetHighlight(): void {
  const state = currentPageState
  const pending = hyperlinkTargetHighlight
  if (!state || !pending || pending.pageNumber !== state.pageNumber) {
    return
  }
  const bounds = targetRegionBounds(
    pending.region,
    state.pageData.page_size.width_pt,
    state.pageData.page_size.height_pt,
  )
  if (!bounds) {
    setStatus(`Page ${pending.pageNumber} opened, but its hyperlink target has no usable bbox.`)
    return
  }
  const node = document.createElement('div')
  node.className =
    pending.region.type === 'whole_page'
      ? 'hyperlink-target-highlight is-whole-page'
      : 'hyperlink-target-highlight'
  node.dataset.sourceItemId = pending.sourceItemId
  node.style.left = `${Math.min(bounds.x0, bounds.x1) * state.scale}px`
  node.style.top = `${
    (bounds.topLeft
      ? Math.min(bounds.y0, bounds.y1)
      : state.pageData.page_size.height_pt - Math.max(bounds.y0, bounds.y1)) * state.scale
  }px`
  node.style.width = `${Math.max(Math.abs(bounds.x1 - bounds.x0) * state.scale, 4)}px`
  node.style.height = `${Math.max(Math.abs(bounds.y1 - bounds.y0) * state.scale, 4)}px`
  state.overlay.appendChild(node)
  window.requestAnimationFrame(() => {
    node.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' })
  })
}

function describeTargetRegion(
  region: NonNullable<LinkItem['link']['target']>['target_highlight_region'],
): string {
  if (!region) {
    return 'No target bbox'
  }
  if (region.type === 'whole_page') {
    return 'Whole page'
  }
  const bbox = region.bbox
  if (
    bbox &&
    [bbox.x0, bbox.y0, bbox.x1, bbox.y1].every((value) => typeof value === 'number')
  ) {
    return `${bbox.x0}, ${bbox.y0}, ${bbox.x1}, ${bbox.y1}`
  }
  if ([region.x0, region.y0, region.x1, region.y1].every((value) => typeof value === 'number')) {
    return `${region.x0}, ${region.y0}, ${region.x1}, ${region.y1}`
  }
  if ([region.x, region.y, region.width, region.height].every((value) => typeof value === 'number')) {
    return `x ${region.x}, y ${region.y}, width ${region.width}, height ${region.height}`
  }
  return region.type ?? 'Unknown'
}

function requestHyperlinkNavigation(item: LinkItem): void {
  if (isInfoTraceActive()) {
    setStatus('Info Trace is active. Stop tracing before following normal hyperlinks.')
    renderInfoTraceHighlights()
    return
  }
  const pageNumber = hyperlinkTargetPage(item)
  if (!activeDocument || pageNumber === null || pageNumber < 1 || pageNumber > activeDocument.page_count) {
    setStatus(`Hyperlink ${item.id} has no valid target page in this document.`)
    return
  }
  const targetPage = getPageManifest(pageNumber)
  if (!targetPage) {
    setStatus(`Target page ${pageNumber} is not present in the document manifest.`)
    return
  }
  pendingHyperlinkNavigation = item
  hyperlinkConfirmSummary.textContent = `This hyperlink points to page ${pageNumber}. Do you want to navigate there?`
  renderMeta(hyperlinkConfirmMeta, [
    ['Target page', String(pageNumber)],
    ['Vectors', String(countOrZero(targetPage.item_counts.vector_path))],
    ['Text objects', String(countOrZero(targetPage.item_counts.text))],
    ['Images', String(countOrZero(targetPage.item_counts.image))],
    ['Hyperlinks', String(countOrZero(targetPage.item_counts.link))],
    ['Page size', `${targetPage.page_size.width_pt} × ${targetPage.page_size.height_pt} pt`],
    ['Target region', describeTargetRegion(item.link.target?.target_highlight_region)],
  ])
  hyperlinkConfirmDialog.showModal()
  hyperlinkConfirmFollowBtn.focus()
}

async function followHyperlink(item: LinkItem): Promise<void> {
  const pageNumber = hyperlinkTargetPage(item)
  const region = item.link.target?.target_highlight_region
  if (!activeDocument || pageNumber === null || pageNumber < 1 || pageNumber > activeDocument.page_count) {
    setStatus(`Hyperlink ${item.id} has no valid target page in this document.`)
    return
  }
  const sourcePageData = currentPageState?.pageData
  const extractedRelation = sourcePageData
    ? extractedCrosspageRelationForReaderLink(item, sourcePageData)
    : undefined
  const targetComponent = extractedRelation?.target_component
  hyperlinkTargetHighlight = region
    ? { pageNumber, region, sourceItemId: item.id }
    : null
  await selectPage(pageNumber, { preserveHyperlinkHighlight: true })
  const focusedTarget = targetComponent !== undefined
    ? focusExtractComponentById(targetComponent)
    : false
  const navigationStatus = region
    ? `Opened hyperlink target on page ${pageNumber} and highlighted its ${region.type ?? 'target'} region.`
    : `Opened hyperlink target page ${pageNumber}; no target bbox was provided.`
  setStatus(
    focusedTarget
      ? `${navigationStatus} Expanded component ${String(targetComponent)} in its JSON.`
      : targetComponent !== undefined
        ? `${navigationStatus} Target component ${String(targetComponent)} was not found in the page JSON.`
        : navigationStatus,
  )
}

function createOverlayItem(item: ReaderItem, scale: number, pageHeight: number): HTMLDivElement {
  const node = document.createElement('div')
  const overlayClass =
    item.kind === 'link'
      ? 'link'
      : item.kind === 'text'
        ? 'text'
        : item.kind === 'image'
          ? 'image'
          : 'vector'
  node.className = `overlay-item overlay-${overlayClass}`
  node.dataset.itemId = item.id
  node.dataset.baseZIndex =
    item.kind === 'link' ? '5' : item.kind === 'text' ? '4' : item.kind === 'image' ? '3' : '2'
  node.title =
    item.kind === 'link'
      ? `${item.link.kind ?? 'link'} | ${item.source.object_ref ?? 'no ref'} | Right-click to follow`
      : item.kind === 'text'
        ? `${item.text.content} | ${item.source.object_ref ?? 'no ref'}`
        : item.kind === 'image'
          ? `${item.image.name} | ${item.image.object_ref}`
          : `${item.paint_operator} | ${item.source.object_ref ?? 'no ref'}`

  const bounds = getRenderedBounds(item, scale, pageHeight)

  node.style.left = `${bounds.left}px`
  node.style.top = `${bounds.top}px`
  node.style.width = `${bounds.width}px`
  node.style.height = `${bounds.height}px`
  node.style.zIndex = node.dataset.baseZIndex
  if (item.kind === 'link') {
    node.addEventListener('contextmenu', (event) => {
      event.preventDefault()
      event.stopPropagation()
      requestHyperlinkNavigation(item)
    })
  }

  return node
}

async function renderPage(pageNumber: number, options: RenderPageOptions = {}): Promise<void> {
  if (!activeDocument) {
    return
  }

  const pageManifest = getPageManifest(pageNumber)
  if (!pageManifest) {
    throw new Error(`Page ${pageNumber} not found.`)
  }

  const renderId = ++renderSequence
  if (options.anchor) {
    // Keep the last valid anchor available for wheel events fired while this
    // asynchronous render has no currentPageState.
    pendingZoomAnchor = options.anchor
  }
  setStatus(`Rendering page ${pageNumber}...`)
  viewerStage.innerHTML = `
    <div class="viewer-loading">
      <div class="viewer-loading-spinner"></div>
      <span>Loading page ${pageNumber}...</span>
    </div>
  `
  currentPageState = null
  if (!options.preserveSelection && !activeVectorMatcherSelection) {
    setSelection(null, null)
  }

  const [pageData, pdf] = await Promise.all([
    loadPageData(activeDocument.id, pageManifest),
    getPdfDocument(activeDocument.pdf_url),
  ])
  if (renderId !== renderSequence) {
    return
  }
  if (activeSelectionId) {
    const selected = pageData.items.find((item) => item.id === activeSelectionId)
    if (selected && !isLayerVisible(selected.kind)) {
      activeSelectionId = null
    }
  }
  const pdfPage = await pdf.getPage(pageNumber)
  if (renderId !== renderSequence) {
    return
  }
  const desiredWidth = Math.min(getAvailableViewerWidth(), 1400)
  const baseViewport = pdfPage.getViewport({ scale: 1 })
  fitScale = desiredWidth / baseViewport.width
  const scale = fitScale * zoomFactor
  const viewport = pdfPage.getViewport({ scale })

  const pageShell = document.createElement('section')
  pageShell.className = 'page-shell'
  pageShell.dataset.pageNumber = String(pageNumber)

  const skipVectorOverlay = vectorOverlaySuppressedPage === pageNumber

  const pageHeader = document.createElement('div')
  pageHeader.className = 'page-header'
  pageHeader.innerHTML = `
    <div>
      <strong>Page ${pageNumber}</strong>
      <span>${countOrZero(pageManifest.item_counts.vector_path)} vector, ${countOrZero(pageManifest.item_counts.text)} text, ${countOrZero(pageManifest.item_counts.image)} image, ${countOrZero(pageManifest.item_counts.link)} link</span>
    </div>
  `
  if (skipVectorOverlay) {
    const notice = document.createElement('div')
    notice.className = 'page-vector-suppressed-notice'
    const label = document.createElement('span')
    label.textContent = 'Vector overlay hidden for performance.'
    const loadVectorsBtn = document.createElement('button')
    loadVectorsBtn.type = 'button'
    loadVectorsBtn.className = 'button'
    loadVectorsBtn.textContent = 'Load vectors anyway'
    loadVectorsBtn.addEventListener('click', () => {
      vectorOverlaySuppressedPage = null
      scheduleRender(pageNumber, { preserveSelection: true })
    })
    notice.append(label, loadVectorsBtn)
    pageHeader.appendChild(notice)
  }
  pageShell.appendChild(pageHeader)

  const surface = document.createElement('div')
  surface.className = 'page-surface'
  surface.style.width = `${viewport.width}px`
  surface.style.height = `${viewport.height}px`

  const canvas = document.createElement('canvas')
  canvas.width = Math.floor(viewport.width)
  canvas.height = Math.floor(viewport.height)
  canvas.style.width = `${viewport.width}px`
  canvas.style.height = `${viewport.height}px`

  const overlay = document.createElement('div')
  overlay.className = 'page-overlay'
  overlay.classList.toggle('is-info-tracing', isInfoTraceActive())
  overlay.style.width = `${viewport.width}px`
  overlay.style.height = `${viewport.height}px`
  overlay.addEventListener('click', (event) => {
    if (isAreaSelectMode) {
      return
    }
    const rect = overlay.getBoundingClientRect()
    const hitX = event.clientX - rect.left
    const hitY = event.clientY - rect.top
    selectItemAtPoint(pageData, hitX, hitY, scale)
  })

  surface.append(canvas, overlay)
  pageShell.appendChild(surface)
  viewerStage.innerHTML = ''
  viewerStage.appendChild(pageShell)

  const ctx = canvas.getContext('2d')
  if (!ctx) {
    throw new Error('Canvas 2D context not available.')
  }

  await pdfPage.render({ canvas: null, canvasContext: ctx, viewport }).promise
  if (renderId !== renderSequence) {
    return
  }

  const items = [...pageData.items].sort((a, b) => compareHitPriority(b, a))
  items.forEach((item) => {
    if (!isLayerVisible(item.kind)) {
      return
    }
    if (skipVectorOverlay && item.kind === 'vector_path') {
      return
    }
    overlay.appendChild(createOverlayItem(item, scale, pageData.page_size.height_pt))
  })
  attachAreaSelectionHandlers(overlay, pageData, scale)

  currentPageState = {
    pageNumber,
    pageData,
    canvas,
    overlay,
    scale,
  }
  renderVectorMatchOverlays()
  renderSplitPageOverlays()
  renderVectorEntityOverlays()
  renderVectorBoxOverlays()
  renderVectorCircleOverlays()
  renderVectorDashedOverlays()
  renderVectorCellOverlays()
  renderVectorPinOverlays()
  renderExtractVectorHighlights()
  renderInfoTraceHighlights()
  renderHyperlinkTargetHighlight()

  renderMeta(pageMeta, [
    ['Page object', pageData.page_object_ref ?? 'None'],
    ['Content streams', pageData.content_streams.join(', ') || 'None'],
    ['Vector objects', String(countOrZero(pageData.item_counts.vector_path))],
    ['Text objects', String(countOrZero(pageData.item_counts.text))],
    ['Image objects', String(countOrZero(pageData.item_counts.image))],
    ['Link objects', String(countOrZero(pageData.item_counts.link))],
    ['Page size (pt)', `${pageData.page_size.width_pt} x ${pageData.page_size.height_pt}`],
  ])

  const renderAnchor = options.anchor ?? pendingZoomAnchor
  applyAnchorScroll(renderAnchor, scale)
  // A later wheel event may already have queued another render. Do not clear
  // its fallback anchor just because this render happened to finish first.
  if (pendingZoomAnchor === renderAnchor && scheduledRenderTimer === null) {
    pendingZoomAnchor = null
  }
  updateZoomLabel()

  const selectedItem = activeSelectionId ? pageData.items.find((item) => item.id === activeSelectionId) ?? null : null
  if (activeVectorMatcherSelection && activeVectorMatcherSelection.pageNumber === pageData.page_number) {
    setVectorMatcherSelection(activeVectorMatcherSelection, true)
  } else if (selectedItem) {
    setSelection(selectedItem, pageData)
  } else if (pageData.warnings.length) {
    warningsEl.innerHTML = pageData.warnings
      .map((warning) => `<div class="warning-card">${escapeHtml(warning)}</div>`)
      .join('')
  } else if (!options.preserveSelection) {
    warningsEl.innerHTML = ''
  }

  setStatus(
    `Page ${pageNumber} rendered. Click a highlighted region to inspect its PDF source; right-click a hyperlink to follow it.`,
  )
  syncSelectionClasses()
  updateVectorMatchButton()
}

function populateDocumentSelect(documents: ReaderDocument[]): void {
  docSelect.innerHTML = documents
    .map((doc) => `<option value="${escapeHtml(doc.id)}">${escapeHtml(doc.title)}</option>`)
    .join('')
}

function populatePageSelect(documentData: ReaderDocument): void {
  pageSelect.innerHTML = documentData.pages
    .map(
      (page) =>
        `<option value="${page.page_number}">Page ${page.page_number} | Vector ${countOrZero(page.item_counts.vector_path)} | Text ${countOrZero(page.item_counts.text)} | Image ${countOrZero(page.item_counts.image)} | Links ${countOrZero(page.item_counts.link)}</option>`,
    )
    .join('')
}

async function selectDocument(documentId: string): Promise<void> {
  if (!manifest) {
    return
  }
  const documentData = manifest.documents.find((doc) => doc.id === documentId)
  if (!documentData) {
    throw new Error(`Document ${documentId} not found.`)
  }

  activeDocument = documentData
  activePageNumber = 1
  vectorOverlaySuppressedPage = null
  pendingZoomAnchor = null
  hyperlinkTargetHighlight = null
  infoTraceIndexData = null
  infoTraceIndexDocumentId = null
  infoTraceIndexPromise = null
  infoTraceAllSteps = []
  infoTraceOpenEndpoints = []
  infoTraceResolvedEndpoints = []
  pendingInfoTraceNavigation = null
  if (infoTraceStopDialog.open) infoTraceStopDialog.close()
  infoTracePageInput.value = '1'
  infoTracePageInput.removeAttribute('max')
  infoTraceIdSelect.innerHTML = '<option value="">Select an ID</option>'
  infoTraceIdSelect.disabled = true
  infoTraceChooseBtn.disabled = true
  infoTraceStopBtn.disabled = true
  infoTraceStatus.textContent = 'Open this tab to load the document trace graph.'
  renderInfoTraceSteps()
  clearSymbolMatches()
  lastExtractedPages = []
  lastExtractedSymbolCount = 0
  hasSymbolExtractionResult = false
  symbolList.replaceChildren()
  setSymbolStatus('Enter the symbol overview page numbers, then start extraction.')
  extractInfoResultData = null
  extractTextOwnership = []
  clearExtractVectorHighlights()
  extractInfoResult.replaceChildren()
  symbolExtractBtn.textContent = 'Start extraction'
  extractInfoRunBtn.textContent = 'Extract'
  updateExtractInfoAvailability()
  zoomFactor = 1
  playgroundStash = null
  updatePlaygroundStashCount(0)
  clearVectorMatcherSelection()
  clearSplitPageResult()
  clearVectorEntityResult()
  clearVectorBoxResult()
  clearVectorCircleResult()
  clearVectorDashedResult()
  clearVectorCellResult()
  clearVectorPinResult()
  populatePageSelect(documentData)
  pageSelect.value = '1'
  renderMeta(docMeta, [
    ['PDF header', documentData.header],
    ['Pages', String(documentData.page_count)],
    ['Resolved objects', String(documentData.resolved_object_count)],
    ['PDF URL', documentData.pdf_url],
  ])
  updateZoomLabel()
  await renderPage(activePageNumber)
  await restorePersistedState(activePageNumber)
}

async function selectPage(
  pageNumber: number,
  options: { preserveHyperlinkHighlight?: boolean } = {},
): Promise<void> {
  if (!activeDocument) {
    return
  }
  if (pageNumber !== activePageNumber) {
    const decision = await confirmLargePageNavigation(pageNumber)
    if (decision === 'cancel') {
      pageSelect.value = String(activePageNumber)
      return
    }
    vectorOverlaySuppressedPage = decision === 'skip-vectors' ? pageNumber : null
  }
  if (!options.preserveHyperlinkHighlight) {
    hyperlinkTargetHighlight = null
  }
  pendingZoomAnchor = null
  activePageNumber = pageNumber
  pageSelect.value = String(pageNumber)
  if (infoTraceIndexData && infoTraceAllSteps.length === 0) {
    infoTracePageInput.value = String(pageNumber)
    populateInfoTraceIds()
  }
  clearSplitPageResult()
  clearVectorEntityResult()
  clearVectorBoxResult()
  clearVectorCircleResult()
  clearVectorDashedResult()
  clearVectorCellResult()
  clearVectorPinResult()
  await renderPage(pageNumber)
  await restorePersistedState(pageNumber)
}

function updatePagerButtons(): void {
  if (!activeDocument) {
    prevPageButton.disabled = true
    nextPageButton.disabled = true
    return
  }
  prevPageButton.disabled = activePageNumber <= 1
  nextPageButton.disabled = activePageNumber >= activeDocument.page_count
}

function changeZoom(nextZoomFactor: number, anchor: ZoomAnchor | null): void {
  const clampedZoom = clamp(nextZoomFactor, MIN_ZOOM_FACTOR, MAX_ZOOM_FACTOR)
  if (Math.abs(clampedZoom - zoomFactor) < 0.001) {
    return
  }
  // Rendering a PDF page is asynchronous and temporarily clears
  // currentPageState. Keep the last valid anchor across that gap so rapid
  // wheel events cannot schedule an unanchored render that resets to (0, 0).
  const effectiveAnchor = anchor ?? pendingZoomAnchor
  if (effectiveAnchor) {
    pendingZoomAnchor = effectiveAnchor
  }
  zoomFactor = clampedZoom
  updateZoomLabel()
  scheduleRender(activePageNumber, { preserveSelection: true, anchor: effectiveAnchor })
}

docSelect.addEventListener('change', async () => {
  await selectDocument(docSelect.value)
  updatePagerButtons()
})

pageSelect.addEventListener('change', async () => {
  await selectPage(Number(pageSelect.value))
  updatePagerButtons()
})

prevPageButton.addEventListener('click', async () => {
  if (activePageNumber > 1) {
    await selectPage(activePageNumber - 1)
    updatePagerButtons()
  }
})

nextPageButton.addEventListener('click', async () => {
  if (activeDocument && activePageNumber < activeDocument.page_count) {
    await selectPage(activePageNumber + 1)
    updatePagerButtons()
  }
})

areaSelectToggleBtn.addEventListener('click', () => {
  if (isInfoTraceActive()) {
    setStatus('Stop Info Trace before enabling area selection.')
    return
  }
  setAreaSelectMode(!isAreaSelectMode)
  if (isAreaSelectMode) {
    setStatus('Area select mode enabled. Drag a rectangle on the page to select vector objects.')
  } else {
    setStatus(`Page ${activePageNumber} ready.`)
  }
})

splitPageDetectBtn.addEventListener('click', async () => {
  if (!activeDocument || !currentPageState) {
    return
  }
  if (splitPageResult?.page_number === activePageNumber) {
    clearSplitPageResult()
    setStatus(`Split page overlays hidden on page ${activePageNumber}.`)
    return
  }
  const cachedSplitPage = getPageDetectionCache()?.splitPage
  if (cachedSplitPage) {
    splitPageResult = cachedSplitPage
    updateDetectButtons()
    renderSplitPageOverlays()
    setStatus(`Restored cached split page boxes on page ${activePageNumber}.`)
    return
  }
  try {
    splitPageDetectBtn.disabled = true
    splitPageResult = null
    updateDetectButtons()
    renderSplitPageOverlays()
    setStatus(`Detecting drawing and information boxes on page ${activePageNumber}...`)
    splitPageResult = await callSplitPageDetection()
    getPageDetectionCache(true)!.splitPage = splitPageResult
    updateDetectButtons()
    renderSplitPageOverlays()
    setStatus(`Detected drawing and information boxes on page ${activePageNumber}.`)
  } catch (error) {
    splitPageResult = null
    updateDetectButtons()
    renderSplitPageOverlays()
    setStatus(`Split page detection failed: ${error instanceof Error ? error.message : String(error)}`)
  } finally {
    splitPageDetectBtn.disabled = false
    updateDetectButtons()
  }
})

entityDetectBtn.addEventListener('click', async () => {
  if (!activeDocument || !currentPageState) {
    return
  }
  if (vectorEntityResult?.page_number === activePageNumber) {
    clearVectorEntityResult()
    setStatus(`Entity overlays hidden on page ${activePageNumber}.`)
    return
  }
  const cachedEntities = getPageDetectionCache()?.entities
  if (cachedEntities) {
    vectorEntityResult = cachedEntities
    activeVectorEntityIndex = null
    updateDetectButtons()
    renderVectorEntityOverlays()
    setStatus(`Restored ${cachedEntities.entity_count} cached vector entities on page ${activePageNumber}.`)
    return
  }
  try {
    entityDetectBtn.disabled = true
    vectorEntityResult = null
    activeVectorEntityIndex = null
    updateDetectButtons()
    renderVectorEntityOverlays()
    setStatus(`Detecting vector entities on page ${activePageNumber}...`)
    vectorEntityResult = await callVectorEntityDetection()
    getPageDetectionCache(true)!.entities = vectorEntityResult
    updateDetectButtons()
    renderVectorEntityOverlays()
    setStatus(
      `Detected ${vectorEntityResult.entity_count} vector entit${vectorEntityResult.entity_count === 1 ? 'y' : 'ies'} inside the drawing area.`,
    )
  } catch (error) {
    vectorEntityResult = null
    activeVectorEntityIndex = null
    updateDetectButtons()
    renderVectorEntityOverlays()
    setStatus(`Entity detection failed: ${error instanceof Error ? error.message : String(error)}`)
  } finally {
    entityDetectBtn.disabled = false
    updateDetectButtons()
  }
})

async function togglePinDetection(kind: 'circle' | 'arrow' | 'wire_mark'): Promise<void> {
  if (!activeDocument || !currentPageState) return
  const group = getSelectedVectorEntityGroup()
  if (!group || activeVectorEntityIndex === null) {
    updateVectorEntityActionButtons()
    setStatus(`Select a vector entity before detecting ${kind === 'wire_mark' ? 'wire marks' : `${kind} ports`}.`)
    return
  }
  const button = kind === 'circle' ? pinCircleDetectBtn : kind === 'arrow' ? pinArrowDetectBtn : wireMarkDetectBtn
  const current = kind === 'circle' ? vectorPinCircleResult : kind === 'arrow' ? vectorPinArrowResult : vectorWireMarkResult
  if (current?.page_number === activePageNumber && current.entity_root === group.root) {
    clearVectorPinResult(kind)
    setStatus(`${kind === 'circle' ? 'Circle pin' : kind === 'arrow' ? 'Arrow pin' : 'Wire mark'} overlays hidden for entity ${activeVectorEntityIndex + 1}.`)
    return
  }
  const cache = getPageDetectionCache()
  const cached = kind === 'circle'
    ? cache?.pinCircles.get(group.root)
    : kind === 'arrow'
      ? cache?.pinArrows.get(group.root)
      : cache?.wireMarks.get(group.root)
  if (cached) {
    if (kind === 'circle') vectorPinCircleResult = cached
    else if (kind === 'arrow') vectorPinArrowResult = cached
    else vectorWireMarkResult = cached
    renderVectorPinOverlays()
    const cachedLabel = kind === 'wire_mark' ? 'wire mark' : `${kind} pin`
    setStatus(`Restored ${cached.pin_count} ${cachedLabel}${cached.pin_count === 1 ? '' : 's'} for entity ${activeVectorEntityIndex + 1}.`)
    return
  }
  try {
    button.disabled = true
    setStatus(`Detecting ${kind === 'wire_mark' ? 'wire marks' : `${kind} ports`} inside entity ${activeVectorEntityIndex + 1}...`)
    const result = await callVectorPinDetection(kind, group, activeVectorEntityIndex)
    if (kind === 'circle') {
      vectorPinCircleResult = result
      getPageDetectionCache(true)!.pinCircles.set(group.root, result)
    } else if (kind === 'arrow') {
      vectorPinArrowResult = result
      getPageDetectionCache(true)!.pinArrows.set(group.root, result)
    } else {
      vectorWireMarkResult = result
      getPageDetectionCache(true)!.wireMarks.set(group.root, result)
    }
    renderVectorPinOverlays()
    const resultLabel = kind === 'wire_mark' ? 'wire mark' : `${kind} port`
    setStatus(`Detected ${result.pin_count} ${resultLabel}${result.pin_count === 1 ? '' : 's'} inside entity ${activeVectorEntityIndex + 1}.`)
  } catch (error) {
    clearVectorPinResult(kind)
    setStatus(`${kind} pin detection failed: ${error instanceof Error ? error.message : String(error)}`)
  } finally {
    updateVectorEntityActionButtons()
  }
}

pinCircleDetectBtn.addEventListener('click', () => void togglePinDetection('circle'))
pinArrowDetectBtn.addEventListener('click', () => void togglePinDetection('arrow'))
wireMarkDetectBtn.addEventListener('click', () => void togglePinDetection('wire_mark'))
symbolMatchDisplayBtn.addEventListener('click', () => {
  showSymbolMatches = !showSymbolMatches
  renderSymbolMatchOverlays()
  updateDetectButtons()
  setStatus(`${showSymbolMatches ? 'Showing' : 'Hiding'} detected symbol matches.`)
})

// ---- Inspector "Symbols" tab: symbol overview extraction ----

interface ApiSymbolShape {
  type: string | null
  points: Array<[number, number]>
  dashed?: boolean
}

interface ApiSymbol {
  shapes: ApiSymbolShape[]
  width: number
  height: number
}

interface ApiSymbolRecord {
  symbol: number
  name: string
  description: string
}

interface SymbolExtractResult {
  category: 'symbols'
  symbols: ApiSymbol[]
  records: ApiSymbolRecord[]
  pages: number[]
}

interface ApiSymbolSearchMatch {
  symbol: number
  boxes: BBox[]
}

interface SymbolSearchResult {
  category: 'symbol_search'
  page: number
  pages: number[]
  entity_root: number
  symbol_count: number
  matches: ApiSymbolSearchMatch[]
}

interface SymbolMatchBox {
  symbol: number
  pageNumber: number
  entityRoot: number
  bbox: BBox
}

type InspectorTab = 'symbols' | 'extract-info' | 'info-trace' | 'source'

function setInspectorTab(tab: InspectorTab): void {
  for (const button of inspectorTabs) {
    const isActive = button.dataset.tab === tab
    button.classList.toggle('is-active', isActive)
    button.setAttribute('aria-selected', String(isActive))
  }
  const symbolsPanel = document.getElementById('tab-panel-symbols')
  const extractInfoPanel = document.getElementById('tab-panel-extract-info')
  const infoTracePanel = document.getElementById('tab-panel-info-trace')
  const sourcePanel = document.getElementById('tab-panel-source')
  if (symbolsPanel) symbolsPanel.hidden = tab !== 'symbols'
  if (extractInfoPanel) extractInfoPanel.hidden = tab !== 'extract-info'
  if (infoTracePanel) infoTracePanel.hidden = tab !== 'info-trace'
  if (sourcePanel) sourcePanel.hidden = tab !== 'source'
  if (tab === 'info-trace') void ensureInfoTraceIndex()
}

for (const button of inspectorTabs) {
  button.addEventListener('click', () => {
    const tab = button.dataset.tab
    setInspectorTab(
      tab === 'source'
        ? 'source'
        : tab === 'extract-info'
          ? 'extract-info'
          : tab === 'info-trace'
            ? 'info-trace'
            : 'symbols',
    )
  })
}

function infoTraceNodeKey(node: Pick<InfoTraceNode, 'page' | 'kind' | 'id'>): string {
  return `${node.page}:${node.kind}:${String(node.id)}`
}

function isInfoTraceActive(): boolean {
  return infoTraceAllSteps.length > 0
}

function blockNonTraceHighlight(): boolean {
  if (!isInfoTraceActive()) return false
  renderInfoTraceHighlights()
  return true
}

function infoTracePage(pageNumber: number): InfoTracePageIndex | undefined {
  return infoTraceIndexData?.pages.find((page) => page.page_number === pageNumber)
}

function infoTraceEntityNode(
  pageNumber: number,
  kind: InfoTraceKind,
  entity: InfoTraceEntity,
): InfoTraceNode {
  const stringList = (value: unknown): string[] => Array.isArray(value)
    ? value.map(String)
    : value === undefined || value === null || value === ''
      ? []
      : [String(value)]
  return {
    page: pageNumber,
    kind,
    id: entity.id,
    bbox: entity.bbox ?? null,
    title: stringList(entity.title),
    descriptions: stringList(entity.descriptions),
  }
}

function findInfoTraceNode(
  pageNumber: number,
  kind: InfoTraceKind,
  id: number | string,
): InfoTraceNode | null {
  const page = infoTracePage(pageNumber)
  const collection = kind === 'component' ? page?.components : page?.wires
  const entity = collection?.find((candidate) => String(candidate.id) === String(id))
  return entity ? infoTraceEntityNode(pageNumber, kind, entity) : null
}

function infoTraceEndpointKey(endpoint: { page: number; id: number | string }): string {
  return `${endpoint.page}:endpoint:${String(endpoint.id)}`
}

function tracedInfoTraceNodeKeys(): Set<string> {
  return new Set(infoTraceAllSteps.flatMap((step) => step.nodes.map(infoTraceNodeKey)))
}

function computeInfoTraceEndpointState(tracedKeys: Set<string>): InfoTraceEndpointState {
  const open: InfoTraceOpenEndpoint[] = []
  const resolved: Array<{ page: number; id: number | string }> = []
  for (const page of infoTraceIndexData?.pages ?? []) {
    const adjacency = new Map<string, Set<string>>()
    for (const relation of page.relations) {
      if (relation.type !== 'connection') continue
      const source = String(relation.source ?? '')
      const target = String(relation.target ?? '')
      const endpointRef = source.startsWith('endpoint:')
        ? source
        : target.startsWith('endpoint:')
          ? target
          : ''
      const nodeRef = endpointRef === source ? target : endpointRef === target ? source : ''
      if (!endpointRef || !/^(component|wire):/.test(nodeRef)) continue
      const refs = adjacency.get(endpointRef) ?? new Set<string>()
      refs.add(nodeRef)
      adjacency.set(endpointRef, refs)
    }
    for (const [endpointRef, nodeRefs] of adjacency) {
      const adjacentNodes = [...nodeRefs]
        .map((nodeRef) => {
          const match = /^(component|wire):(.*)$/.exec(nodeRef)
          return match ? findInfoTraceNode(page.page_number, match[1] as InfoTraceKind, match[2]) : null
        })
        .filter((node): node is InfoTraceNode => Boolean(node))
      const tracedNodes = adjacentNodes.filter((node) => tracedKeys.has(infoTraceNodeKey(node)))
      if (tracedNodes.length === 0) continue
      const untracedNodes = adjacentNodes.filter((node) => !tracedKeys.has(infoTraceNodeKey(node)))
      const endpoint = {
        page: page.page_number,
        id: endpointRef.slice('endpoint:'.length),
      }
      if (untracedNodes.length > 0) {
        open.push({ ...endpoint, sources: tracedNodes, candidates: untracedNodes })
      }
      if (tracedNodes.length >= 2) resolved.push(endpoint)
    }
  }
  const compare = (
    left: { page: number; id: number | string },
    right: { page: number; id: number | string },
  ): number => left.page - right.page
    || String(left.id).localeCompare(String(right.id), undefined, { numeric: true })
  open.sort(compare)
  resolved.sort(compare)
  return { open, resolved }
}

function refreshInfoTraceEndpointState(): void {
  const state = computeInfoTraceEndpointState(tracedInfoTraceNodeKeys())
  infoTraceOpenEndpoints = state.open
  infoTraceResolvedEndpoints = state.resolved
}

function infoTraceNetExpansion(
  sourceKeys: Set<string>,
  tracedKeys: Set<string>,
): {
  nodes: InfoTraceNode[]
  nets: Array<{ page: number; id: number | string }>
} {
  const nodes = new Map<string, InfoTraceNode>()
  const nets = new Map<string, { page: number; id: number | string }>()
  for (const page of infoTraceIndexData?.pages ?? []) {
    const netWires = new Map<string, Set<string>>()
    for (const relation of page.relations) {
      if (relation.type !== 'contains') continue
      const source = String(relation.source ?? '')
      const target = String(relation.target ?? '')
      if (!source.startsWith('net:') || !target.startsWith('wire:')) continue
      const wireRefs = netWires.get(source) ?? new Set<string>()
      wireRefs.add(target)
      netWires.set(source, wireRefs)
    }
    for (const [netRef, wireRefs] of netWires) {
      const containsSelectedWire = [...wireRefs].some((wireRef) => (
        sourceKeys.has(`${page.page_number}:${wireRef}`)
      ))
      if (!containsSelectedWire) continue
      let expanded = false
      for (const wireRef of wireRefs) {
        const node = findInfoTraceNode(page.page_number, 'wire', wireRef.slice('wire:'.length))
        if (!node || tracedKeys.has(infoTraceNodeKey(node))) continue
        nodes.set(infoTraceNodeKey(node), node)
        expanded = true
      }
      if (expanded) {
        const net = { page: page.page_number, id: netRef.slice('net:'.length) }
        nets.set(`${net.page}:${String(net.id)}`, net)
      }
    }
  }
  return { nodes: [...nodes.values()], nets: [...nets.values()] }
}

function infoTraceTransferExpansion(
  sourceKeys: Set<string>,
  tracedKeys: Set<string>,
): InfoTraceNode[] {
  const nodes = new Map<string, InfoTraceNode>()
  for (const transfer of infoTraceIndexData?.transfers ?? []) {
    const source = transfer.source_component === null
      ? null
      : findInfoTraceNode(transfer.source_page, 'component', transfer.source_component)
    const target = transfer.target_component === undefined
      ? null
      : findInfoTraceNode(transfer.target_page, 'component', transfer.target_component)
    if (!source || !target) continue
    const sourceSelected = sourceKeys.has(infoTraceNodeKey(source))
    const targetSelected = sourceKeys.has(infoTraceNodeKey(target))
    const sourceVisited = tracedKeys.has(infoTraceNodeKey(source))
    const targetVisited = tracedKeys.has(infoTraceNodeKey(target))
    if (sourceSelected && !targetVisited) nodes.set(infoTraceNodeKey(target), target)
    if (targetSelected && !sourceVisited) nodes.set(infoTraceNodeKey(source), source)
  }
  return [...nodes.values()]
}

function nextInfoTraceStep(): InfoTraceStep | null {
  const currentStep = infoTraceAllSteps.at(-1)
  if (!currentStep) return null
  const currentNodeKeys = new Set(currentStep.nodes.map(infoTraceNodeKey))
  const sourceKeys = new Set(
    currentStep.selectedNodeKeys.filter((key) => currentNodeKeys.has(key)),
  )
  if (sourceKeys.size === 0) return null
  const tracedKeys = tracedInfoTraceNodeKeys()
  const endpointState = computeInfoTraceEndpointState(tracedKeys)
  const netExpansion = infoTraceNetExpansion(sourceKeys, tracedKeys)
  const nodes = new Map<string, InfoTraceNode>()
  const nets = new Map<string, { page: number; id: number | string }>()
  for (const net of netExpansion.nets) nets.set(`${net.page}:${String(net.id)}`, net)
  if (netExpansion.nodes.length > 0) {
    for (const node of netExpansion.nodes) nodes.set(infoTraceNodeKey(node), node)
  } else {
    const componentConnectedWireKeys = new Set<string>()
    for (const endpoint of endpointState.open) {
      const selectedSources = endpoint.sources
        .filter((node) => sourceKeys.has(infoTraceNodeKey(node)))
      if (selectedSources.length === 0) continue
      const expandsFromComponent = selectedSources.some((node) => node.kind === 'component')
      for (const node of endpoint.candidates) {
        nodes.set(infoTraceNodeKey(node), node)
        if (expandsFromComponent && node.kind === 'wire') {
          componentConnectedWireKeys.add(infoTraceNodeKey(node))
        }
      }
    }
    if (componentConnectedWireKeys.size > 0) {
      const connectedNetExpansion = infoTraceNetExpansion(
        componentConnectedWireKeys,
        tracedKeys,
      )
      for (const node of connectedNetExpansion.nodes) {
        nodes.set(infoTraceNodeKey(node), node)
      }
      for (const net of connectedNetExpansion.nets) {
        nets.set(`${net.page}:${String(net.id)}`, net)
      }
    }
    for (const node of infoTraceTransferExpansion(sourceKeys, tracedKeys)) {
      nodes.set(infoTraceNodeKey(node), node)
    }
  }
  // A hop may only contain nodes that have not appeared in any earlier hop.
  // Keep this final guard even though each expansion path already filters its
  // candidates, so an exhausted trace can never copy the current hop forward.
  for (const tracedKey of tracedKeys) nodes.delete(tracedKey)
  if (nodes.size === 0) return null
  const nextTracedKeys = new Set([...tracedKeys, ...nodes.keys()])
  const nextEndpointState = computeInfoTraceEndpointState(nextTracedKeys)
  const previousResolved = new Set(endpointState.resolved.map(infoTraceEndpointKey))
  const newlyResolved = nextEndpointState.resolved
    .filter((endpoint) => !previousResolved.has(infoTraceEndpointKey(endpoint)))
  const orderedNodes = [...nodes.values()].sort((left, right) => (
    left.page - right.page
    || left.kind.localeCompare(right.kind)
    || String(left.id).localeCompare(String(right.id), undefined, { numeric: true })
  ))
  return {
    depth: infoTraceAllSteps.length,
    nodes: orderedNodes,
    endpoints: newlyResolved,
    nets: [...nets.values()],
    selectedNodeKeys: [],
  }
}

function renderInfoTraceHighlights(scrollToFirst = false): void {
  const state = currentPageState
  const result = extractInfoResultData
  if (!state || !result || result.page_number !== state.pageNumber || infoTraceAllSteps.length === 0) {
    return
  }
  state.overlay.classList.add('is-info-tracing')
  const componentIds = new Set<string>()
  const wireIds = new Set<string>()
  const endpointIds = new Set<string>()
  for (const step of infoTraceAllSteps) {
    for (const node of step.nodes) {
      if (node.page !== state.pageNumber) continue
      if (node.kind === 'component') componentIds.add(String(node.id))
      else wireIds.add(String(node.id))
    }
  }
  for (const endpointRef of infoTraceResolvedEndpoints) {
    if (endpointRef.page === state.pageNumber) endpointIds.add(String(endpointRef.id))
  }

  // Reuse Extract Info's per-shape rendering: components are the active/red
  // vectors, wires are the related/orange vectors, and endpoints use markers.
  activeExtractHighlightVectors = result.components
    .filter((component) => componentIds.has(String(component.id)))
    .flatMap((component) => component.shape)
  relatedExtractHighlightVectors = result.wires
    .filter((wire) => wireIds.has(String(wire.id)))
    .flatMap((wire) => wire.vectors)
  activeExtractHighlightEndpoints = result.endpoints
    .filter((endpoint) => endpointIds.has(String(endpoint.id)))
  activeExtractHighlightTexts = []
  renderExtractVectorHighlights()
  const currentStep = infoTraceAllSteps.at(-1)
  const selectedKeys = new Set(currentStep?.selectedNodeKeys ?? [])
  const selectedVectors = [
    ...result.components
      .filter((component) => selectedKeys.has(`${state.pageNumber}:component:${String(component.id)}`))
      .flatMap((component) => component.shape),
    ...result.wires
      .filter((wire) => selectedKeys.has(`${state.pageNumber}:wire:${String(wire.id)}`))
      .flatMap((wire) => wire.vectors),
  ]
  for (const vector of selectedVectors) {
    const bbox = vector.bbox
    if (!bbox) continue
    const node = document.createElement('div')
    node.className = 'info-trace-selected-shape-highlight'
    node.style.left = `${bbox.x0 * state.scale}px`
    node.style.top = `${bbox.y0 * state.scale}px`
    node.style.width = `${Math.max((bbox.x1 - bbox.x0) * state.scale, 5)}px`
    node.style.height = `${Math.max((bbox.y1 - bbox.y0) * state.scale, 5)}px`
    state.overlay.appendChild(node)
  }
  if (scrollToFirst) {
    const firstNode = state.overlay.querySelector<HTMLElement>(
      '.info-trace-selected-shape-highlight, .extract-vector-highlight, .extract-endpoint-highlight',
    )
    window.requestAnimationFrame(() => {
      firstNode?.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' })
    })
  }
}

function updateInfoTraceStepSelection(step: InfoTraceStep, selectedKeys: Iterable<string>): void {
  if (step !== infoTraceAllSteps.at(-1)) return
  const allowedKeys = new Set(step.nodes.map(infoTraceNodeKey))
  step.selectedNodeKeys = [...new Set(selectedKeys)]
    .filter((key) => allowedKeys.has(key))
  renderInfoTraceSteps()
  infoTraceStatus.textContent = `${step.selectedNodeKeys.length} of ${step.nodes.length} nodes selected for the next hop.`
}

function renderInfoTraceSteps(): void {
  refreshInfoTraceEndpointState()
  currentPageState?.overlay.classList.toggle('is-info-tracing', isInfoTraceActive())
  infoTraceSteps.replaceChildren()
  for (const step of infoTraceAllSteps) {
    const isLatest = step === infoTraceAllSteps.at(-1)
    const selectedKeys = new Set(step.selectedNodeKeys)
    const section = document.createElement('details')
    section.className = 'info-trace-step'
    section.open = isLatest
    const heading = document.createElement('summary')
    heading.className = 'info-trace-step-summary'
    const stepLabel = step.depth === 0 ? 'Start' : `Hop ${step.depth}`
    const headingLabel = document.createElement('span')
    headingLabel.textContent = `${stepLabel} · ${selectedKeys.size}/${step.nodes.length} selected${step.nets.length > 0 ? ` · ${step.nets.length} net${step.nets.length === 1 ? '' : 's'}` : ''}${step.endpoints.length > 0 ? ` · ${step.endpoints.length} endpoint${step.endpoints.length === 1 ? '' : 's'}` : ''}`
    const selectAllLabel = document.createElement('label')
    selectAllLabel.className = 'info-trace-select-all'
    selectAllLabel.addEventListener('click', (event) => event.stopPropagation())
    const selectAllCheckbox = document.createElement('input')
    selectAllCheckbox.type = 'checkbox'
    selectAllCheckbox.checked = step.nodes.length > 0 && selectedKeys.size === step.nodes.length
    selectAllCheckbox.indeterminate = selectedKeys.size > 0 && selectedKeys.size < step.nodes.length
    selectAllCheckbox.disabled = !isLatest
    selectAllCheckbox.setAttribute('aria-label', `Select all nodes in ${stepLabel}`)
    selectAllCheckbox.addEventListener('click', (event) => event.stopPropagation())
    selectAllCheckbox.addEventListener('change', () => {
      updateInfoTraceStepSelection(
        step,
        selectAllCheckbox.checked ? step.nodes.map(infoTraceNodeKey) : [],
      )
    })
    const selectAllText = document.createElement('span')
    selectAllText.textContent = 'All'
    selectAllLabel.append(selectAllCheckbox, selectAllText)
    heading.append(headingLabel, selectAllLabel)
    section.appendChild(heading)
    const list = document.createElement('div')
    list.className = 'info-trace-step-nodes'
    for (const node of step.nodes) {
      const nodeKey = infoTraceNodeKey(node)
      const row = document.createElement('div')
      row.className = selectedKeys.has(nodeKey)
        ? 'info-trace-node-row is-selected'
        : 'info-trace-node-row'
      const button = document.createElement('button')
      button.type = 'button'
      button.className = 'info-trace-node'
      const label = node.title[0] || node.descriptions[0] || `${node.kind} ${String(node.id)}`
      button.textContent = `P${node.page} · ${node.kind} ${String(node.id)} · ${label}`
      button.addEventListener('click', () => requestInfoTraceNavigation(node))
      const checkbox = document.createElement('input')
      checkbox.type = 'checkbox'
      checkbox.className = 'info-trace-node-checkbox'
      checkbox.checked = selectedKeys.has(nodeKey)
      checkbox.disabled = !isLatest
      checkbox.setAttribute('aria-label', `Use ${node.kind} ${String(node.id)} for the next hop`)
      checkbox.addEventListener('change', () => {
        const nextSelectedKeys = new Set(step.selectedNodeKeys)
        if (checkbox.checked) nextSelectedKeys.add(nodeKey)
        else nextSelectedKeys.delete(nodeKey)
        updateInfoTraceStepSelection(step, nextSelectedKeys)
      })
      row.append(button, checkbox)
      list.appendChild(row)
    }
    section.appendChild(list)
    infoTraceSteps.appendChild(section)
  }
  const latestDepth = Math.max(0, infoTraceAllSteps.length - 1)
  infoTraceDepth.textContent = infoTraceAllSteps.length > 0
    ? `${latestDepth} hop${latestDepth === 1 ? '' : 's'} · ${infoTraceOpenEndpoints.length} free endpoint${infoTraceOpenEndpoints.length === 1 ? '' : 's'}`
    : 'Select a target'
  infoTraceBackBtn.disabled = infoTraceAllSteps.length <= 1
  // Keep Next available while tracing so a click can explain whether the user
  // needs to select a node or whether the selected branch has reached its end.
  infoTraceForwardBtn.disabled = infoTraceAllSteps.length === 0
  renderInfoTraceHighlights()
}

async function chooseInfoTraceRoot(): Promise<void> {
  const pageNumber = Number(infoTracePageInput.value)
  const kind = infoTraceKindSelect.value as InfoTraceKind
  const id = infoTraceIdSelect.value
  const root = id ? findInfoTraceNode(pageNumber, kind, id) : null
  setAreaSelectMode(false)
  activeSelectionId = null
  hyperlinkTargetHighlight = null
  currentPageState?.overlay
    .querySelectorAll('.hyperlink-target-highlight')
    .forEach((node) => node.remove())
  syncSelectionClasses()
  infoTraceAllSteps = root ? [{
    depth: 0,
    nodes: [root],
    endpoints: [],
    nets: [],
    selectedNodeKeys: [],
  }] : []
  renderInfoTraceSteps()
  infoTraceStatus.textContent = root
    ? `Tracing from page ${root.page} ${root.kind} ${String(root.id)} with ${infoTraceOpenEndpoints.length} free endpoint${infoTraceOpenEndpoints.length === 1 ? '' : 's'}.`
    : 'Select a target ID to begin tracing.'
  if (!root) return
  infoTraceStopBtn.disabled = false
  clearExtractVectorHighlights()
  if (activePageNumber !== root.page) await selectPage(root.page)
  renderInfoTraceHighlights(true)
}

function stopInfoTrace(): void {
  infoTraceAllSteps = []
  pendingInfoTraceNavigation = null
  if (infoTraceConfirmDialog.open) infoTraceConfirmDialog.close()
  if (infoTraceStopDialog.open) infoTraceStopDialog.close()
  clearExtractVectorHighlights()
  infoTraceStopBtn.disabled = true
  infoTraceChooseBtn.disabled = !infoTraceIdSelect.value
  infoTraceStatus.textContent = infoTraceIdSelect.value
    ? `Ready to trace ${infoTraceKindSelect.value} ${infoTraceIdSelect.value}. Click Choose to start.`
    : 'Select an ID, then click Choose to begin tracing.'
  renderInfoTraceSteps()
}

function populateInfoTraceIds(): void {
  const page = infoTracePage(Number(infoTracePageInput.value))
  const kind = infoTraceKindSelect.value as InfoTraceKind
  const entities = kind === 'component' ? page?.components ?? [] : page?.wires ?? []
  infoTraceIdSelect.innerHTML = [
    '<option value="">Select an ID</option>',
    ...entities.map((entity) => {
      const label = Array.isArray(entity.title) ? entity.title[0] : entity.title
      return `<option value="${escapeHtml(String(entity.id))}">${escapeHtml(String(entity.id))}${label ? ` · ${escapeHtml(String(label))}` : ''}</option>`
    }),
  ].join('')
  infoTraceIdSelect.disabled = entities.length === 0
  infoTraceChooseBtn.disabled = true
  infoTraceStatus.textContent = entities.length > 0
    ? 'Select an ID, then click Choose to begin tracing.'
    : `No ${kind}s are available on page ${String(page?.page_number ?? infoTracePageInput.value)}.`
}

function populateInfoTraceControls(index: InfoTraceIndex): void {
  infoTracePageInput.max = String(activeDocument?.page_count ?? index.pages.at(-1)?.page_number ?? '')
  infoTracePageInput.value = String(activePageNumber)
  populateInfoTraceIds()
}

async function ensureInfoTraceIndex(): Promise<InfoTraceIndex | null> {
  if (!activeDocument) return null
  if (infoTraceIndexData && infoTraceIndexDocumentId === activeDocument.id) {
    return infoTraceIndexData
  }
  if (infoTraceIndexPromise && infoTraceIndexDocumentId === activeDocument.id) {
    return infoTraceIndexPromise
  }
  const documentAtRequest = activeDocument
  infoTraceIndexDocumentId = documentAtRequest.id
  infoTraceStatus.textContent = 'Loading document trace graph…'
  infoTraceIndexPromise = (async () => {
    try {
      const response = await fetch('/api/vector-matcher', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode: 'trace_index',
          pdf_url: documentAtRequest.pdf_url,
          page: activePageNumber,
        }),
      })
      const payload = (await response.json()) as { ok: boolean; error?: string; result?: InfoTraceIndex }
      if (!response.ok || !payload.ok || !payload.result) {
        throw new Error(payload.error || `HTTP ${response.status}`)
      }
      if (activeDocument !== documentAtRequest) return null
      infoTraceIndexData = payload.result
      populateInfoTraceControls(payload.result)
      infoTraceStatus.textContent = `Loaded ${payload.result.document_result_filename ?? 'document trace graph'}.`
      return payload.result
    } catch (error) {
      if (activeDocument === documentAtRequest) {
        infoTraceStatus.textContent = `Unable to load trace graph: ${errorMessage(error)}`
      }
      return null
    } finally {
      if (activeDocument === documentAtRequest) infoTraceIndexPromise = null
    }
  })()
  return infoTraceIndexPromise
}

function requestInfoTraceNavigation(node: InfoTraceNode): void {
  pendingInfoTraceNavigation = node
  infoTraceConfirmSummary.textContent = `Open page ${node.page} and highlight ${node.kind} ${String(node.id)}?`
  renderMeta(infoTraceConfirmMeta, [
    ['Page', String(node.page)],
    ['Type', node.kind],
    ['ID', String(node.id)],
    ['Title', node.title.join(' · ') || 'None'],
    ['Descriptions', node.descriptions.join(' · ') || 'None'],
  ])
  infoTraceConfirmDialog.showModal()
  infoTraceConfirmFollowBtn.focus()
}

async function followInfoTraceNode(node: InfoTraceNode): Promise<void> {
  if (activePageNumber !== node.page) await selectPage(node.page)
  renderInfoTraceHighlights(true)
  setInspectorTab('info-trace')
  setStatus(`Opened page ${node.page} and highlighted ${node.kind} ${String(node.id)}.`)
}

infoTracePageInput.addEventListener('change', () => {
  const requestedPage = Number.parseInt(infoTracePageInput.value, 10)
  const maxPage = activeDocument?.page_count ?? Number.MAX_SAFE_INTEGER
  infoTracePageInput.value = String(
    Number.isFinite(requestedPage) ? Math.max(1, Math.min(requestedPage, maxPage)) : activePageNumber,
  )
  populateInfoTraceIds()
})
infoTraceKindSelect.addEventListener('change', populateInfoTraceIds)
infoTraceIdSelect.addEventListener('change', () => {
  infoTraceChooseBtn.disabled = !infoTraceIdSelect.value
  infoTraceStatus.textContent = infoTraceIdSelect.value
    ? `Ready to trace ${infoTraceKindSelect.value} ${infoTraceIdSelect.value}. Click Choose to start.`
    : 'Select an ID, then click Choose to begin tracing.'
})
infoTraceChooseBtn.addEventListener('click', () => {
  void chooseInfoTraceRoot()
})
infoTraceStopBtn.addEventListener('click', () => {
  infoTraceStopDialog.showModal()
  infoTraceStopConfirmBtn.focus()
})
infoTraceStopCancelBtn.addEventListener('click', () => {
  infoTraceStopDialog.close()
})
infoTraceStopConfirmBtn.addEventListener('click', stopInfoTrace)
infoTraceForwardBtn.addEventListener('click', () => {
  const currentStep = infoTraceAllSteps.at(-1)
  if (!currentStep) return
  if (currentStep.selectedNodeKeys.length === 0) {
    infoTraceStatus.textContent = 'Select at least one node in the current hop before continuing.'
    return
  }
  const next = nextInfoTraceStep()
  if (!next) {
    const latestDepth = Math.max(0, infoTraceAllSteps.length - 1)
    infoTraceDepth.textContent = `${latestDepth} hop${latestDepth === 1 ? '' : 's'} · End`
    infoTraceForwardBtn.disabled = true
    infoTraceStatus.textContent = 'End of trace: the selected nodes do not lead to any new components or wires.'
    return
  }
  infoTraceAllSteps.push(next)
  renderInfoTraceSteps()
  infoTraceStatus.textContent = `Added hop ${next.depth}. ${infoTraceOpenEndpoints.length} free endpoint${infoTraceOpenEndpoints.length === 1 ? '' : 's'} remain.`
})
infoTraceBackBtn.addEventListener('click', () => {
  if (infoTraceAllSteps.length <= 1) return
  infoTraceAllSteps.pop()
  renderInfoTraceSteps()
  infoTraceStatus.textContent = `Removed the latest hop; now at hop ${infoTraceAllSteps.length - 1} with ${infoTraceOpenEndpoints.length} free endpoint${infoTraceOpenEndpoints.length === 1 ? '' : 's'}.`
})
infoTraceConfirmCancelBtn.addEventListener('click', () => {
  pendingInfoTraceNavigation = null
  infoTraceConfirmDialog.close()
})
infoTraceConfirmDialog.addEventListener('cancel', () => {
  pendingInfoTraceNavigation = null
})
infoTraceConfirmFollowBtn.addEventListener('click', () => {
  const node = pendingInfoTraceNavigation
  pendingInfoTraceNavigation = null
  infoTraceConfirmDialog.close()
  if (node) void followInfoTraceNode(node)
})

function setSymbolStatus(message: string): void {
  symbolStatus.textContent = message
}

function parseSymbolPages(raw: string): number[] {
  const seen = new Set<number>()
  const pages: number[] = []
  for (const token of raw.split(/[^0-9]+/)) {
    if (!token) continue
    const value = Number.parseInt(token, 10)
    if (Number.isFinite(value) && value > 0 && !seen.has(value)) {
      seen.add(value)
      pages.push(value)
    }
  }
  return pages.sort((a, b) => a - b)
}

function clearExtractVectorHighlightNodes(): void {
  currentPageState?.overlay
    .querySelectorAll('.extract-vector-highlight, .extract-endpoint-highlight, .extract-text-highlight, .info-trace-selected-shape-highlight')
    .forEach((node) => node.remove())
}

function clearExtractVectorHighlights(): void {
  activeExtractHighlightVectors = []
  relatedExtractHighlightVectors = []
  activeExtractHighlightEndpoints = []
  activeExtractHighlightTexts = []
  clearExtractVectorHighlightNodes()
}

function endpointMarkerPoint(endpoint: ExtractedEndpoint): [number, number] | null {
  const points = endpoint.shape.flatMap((path) => path.points ?? [])
  if (points.length > 0) {
    const [xTotal, yTotal] = points.reduce(
      ([xSum, ySum], [x, y]) => [xSum + x, ySum + y],
      [0, 0],
    )
    return [xTotal / points.length, yTotal / points.length]
  }
  if (!endpoint.bbox) return null
  const { x0, y0, x1, y1 } = endpoint.bbox
  if ([x0, y0, x1, y1].every((value) => typeof value === 'number')) {
    return [((x0 as number) + (x1 as number)) / 2, ((y0 as number) + (y1 as number)) / 2]
  }
  return null
}

function renderExtractVectorHighlights(): void {
  clearExtractVectorHighlightNodes()
  const st = currentPageState
  if (!st || extractInfoResultData?.page_number !== st.pageNumber) return
  const renderVectors = (vectors: ApiPathBase[], related: boolean): void => {
    for (const vector of vectors) {
      const bbox = vector.bbox
      if (!bbox) continue
      const node = document.createElement('div')
      node.className = related
        ? 'extract-vector-highlight extract-vector-highlight-related'
        : 'extract-vector-highlight'
      node.style.left = `${bbox.x0 * st.scale}px`
      node.style.top = `${bbox.y0 * st.scale}px`
      node.style.width = `${Math.max((bbox.x1 - bbox.x0) * st.scale, 5)}px`
      node.style.height = `${Math.max((bbox.y1 - bbox.y0) * st.scale, 5)}px`
      st.overlay.appendChild(node)
    }
  }
  renderVectors(relatedExtractHighlightVectors, true)
  renderVectors(activeExtractHighlightVectors, false)
  for (const endpoint of activeExtractHighlightEndpoints) {
    const point = endpointMarkerPoint(endpoint)
    if (!point) continue
    const [x, y] = point
    const size = 18
    const node = document.createElement('div')
    node.className = 'extract-endpoint-highlight'
    node.style.left = `${x * st.scale - size / 2}px`
    node.style.top = `${y * st.scale - size / 2}px`
    node.style.width = `${size}px`
    node.style.height = `${size}px`
    node.title = `Endpoint ${String(endpoint.id)} at ${x}, ${y}`
    st.overlay.appendChild(node)
  }
  for (const bbox of activeExtractHighlightTexts) {
    const node = document.createElement('div')
    node.className = 'extract-text-highlight'
    node.style.left = `${bbox.x0 * st.scale}px`
    node.style.top = `${bbox.y0 * st.scale}px`
    node.style.width = `${Math.max((bbox.x1 - bbox.x0) * st.scale, 5)}px`
    node.style.height = `${Math.max((bbox.y1 - bbox.y0) * st.scale, 5)}px`
    st.overlay.appendChild(node)
  }
}

type ExtractNodeKind = 'component' | 'element' | 'group' | 'wire' | 'endpoint'

function extractNodeRef(kind: ExtractNodeKind, id: unknown): string {
  return `${kind}:${String(id)}`
}

function hydrateExtractInfoResult(result: ExtractInfoResult): ExtractInfoResult {
  extractTextOwnership = Array.isArray(result._text_ownership) ? result._text_ownership : []
  delete result._text_ownership
  const diagram = result.diagram
  const elementById = new Map(diagram.elements.map((element) => [String(element.id), element]))
  const relations = diagram.relations ?? []
  const projectEntity = (entity: ExtractedEntity): void => {
    const ownedElements = entity.elements
      .map((id) => elementById.get(String(id)))
      .filter((element): element is ExtractedElement => Boolean(element))
    Object.defineProperties(entity, {
      shape: {
        enumerable: false,
        get: () => ownedElements.flatMap((element) => element.shape),
      },
      vector_indices: {
        enumerable: false,
        get: () => ownedElements.flatMap((element) => element.vector_indices ?? []),
      },
    })
  }
  for (const entity of [
    ...diagram.components,
    ...diagram.endpoints,
    ...diagram.wires,
    ...diagram.nets,
    ...diagram.groups,
  ]) projectEntity(entity)

  const connectedEndpointIds = (nodeRef: string): Array<number | string> => relations
    .filter((edge) => edge.type === 'connection' && (edge.source === nodeRef || edge.target === nodeRef))
    .map((edge) => edge.source.startsWith('endpoint:') ? edge.source : edge.target)
    .filter((ref) => ref.startsWith('endpoint:'))
    .map((ref) => ref.slice('endpoint:'.length))
  for (const component of diagram.components) {
    Object.defineProperty(component, 'endpoints', {
      enumerable: false,
      get: () => connectedEndpointIds(extractNodeRef('component', component.id)),
    })
  }
  for (const wire of diagram.wires) {
    Object.defineProperty(wire, 'vectors', {
      enumerable: false,
      get: () => wire.shape,
    })
  }
  for (const net of diagram.nets) {
    const wireIds = relations
      .filter((edge) => edge.type === 'contains' && edge.source === `net:${String(net.id)}` && edge.target.startsWith('wire:'))
      .map((edge) => edge.target.slice('wire:'.length))
    Object.defineProperties(net, {
      wire_ids: { enumerable: false, get: () => wireIds },
      endpoints: {
        enumerable: false,
        get: () => [...new Set(wireIds.flatMap((wireId) => connectedEndpointIds(`wire:${wireId}`)))],
      },
    })
  }
  for (const [key, value] of Object.entries({
    components: diagram.components,
    elements: diagram.elements,
    groups: diagram.groups,
    wires: diagram.wires,
    endpoints: diagram.endpoints,
    relations: diagram.relations,
    nets: diagram.nets,
  })) {
    Object.defineProperty(result, key, { enumerable: false, get: () => value })
  }
  return result
}

function extractRelations(): ExtractedRelation[] {
  return Array.isArray(extractInfoResultData?.relations) ? extractInfoResultData.relations : []
}

function directlyRelatedExtractNodes(nodeRef: string): Set<string> {
  const related = new Set<string>()
  for (const edge of extractRelations()) {
    const source = String(edge.source ?? '')
    const target = String(edge.target ?? '')
    if (source === nodeRef) related.add(target)
    if (target === nodeRef) related.add(source)
  }
  return related
}

function expandEndpointRelations(nodeRefs: Set<string>): Set<string> {
  const expanded = new Set(nodeRefs)
  for (const endpointRef of nodeRefs) {
    if (!endpointRef.startsWith('endpoint:')) continue
    for (const related of directlyRelatedExtractNodes(endpointRef)) expanded.add(related)
  }
  return expanded
}

function endpointLinkedExtractNodes(primaryRefs: Set<string>): {
  endpointRefs: Set<string>
  relatedRefs: Set<string>
} {
  const endpointRefs = new Set<string>()
  for (const primaryRef of primaryRefs) {
    for (const relatedRef of directlyRelatedExtractNodes(primaryRef)) {
      if (relatedRef.startsWith('endpoint:')) endpointRefs.add(relatedRef)
    }
  }

  const relatedRefs = new Set<string>()
  for (const endpointRef of endpointRefs) {
    for (const relatedRef of directlyRelatedExtractNodes(endpointRef)) {
      if (!primaryRefs.has(relatedRef)) relatedRefs.add(relatedRef)
    }
  }
  return { endpointRefs, relatedRefs }
}

function extractVectorsForNodes(nodeRefs: Set<string>): ApiPathBase[] {
  const result = extractInfoResultData
  if (!result) return []
  return [
    ...result.components
      .filter((component) => nodeRefs.has(extractNodeRef('component', component.id)))
      .flatMap((component) => component.shape),
    ...result.wires
      .filter((wire) => nodeRefs.has(extractNodeRef('wire', wire.id)))
      .flatMap((wire) => wire.vectors),
    ...result.groups
      .filter((group) => nodeRefs.has(extractNodeRef('group', group.id)))
      .flatMap((group) => group.shape),
  ]
}

function extractEndpointsForNodes(nodeRefs: Set<string>): ExtractedEndpoint[] {
  const endpoints = extractInfoResultData?.endpoints
  if (!Array.isArray(endpoints)) return []
  return endpoints.filter((endpoint) => nodeRefs.has(extractNodeRef('endpoint', endpoint.id)))
}

function highlightExtractedItem(
  category: 'component' | 'element' | 'group' | 'wire',
  id: unknown,
  vectors: ApiPathBase[],
): void {
  if (blockNonTraceHighlight()) return
  const nodeRef = extractNodeRef(category, id)
  const relatedNodes = expandEndpointRelations(directlyRelatedExtractNodes(nodeRef))
  relatedNodes.delete(nodeRef)
  activeExtractHighlightEndpoints = extractEndpointsForNodes(relatedNodes)
  activeExtractHighlightTexts = []
  activeExtractHighlightVectors = vectors
  relatedExtractHighlightVectors = extractVectorsForNodes(relatedNodes)
  renderExtractVectorHighlights()
}

function hasSharedEndpoint(
  leftEndpoints: Array<number | string>,
  rightEndpoints: Array<number | string>,
): boolean {
  const leftIds = new Set(leftEndpoints.map(String))
  return rightEndpoints.some((endpointId) => leftIds.has(String(endpointId)))
}

function highlightExtractedNet(net: ExtractedNet): void {
  if (blockNonTraceHighlight()) return
  const result = extractInfoResultData
  if (!result) return
  const selectedIds = new Set(net.wire_ids.map(String))
  activeExtractHighlightEndpoints = extractEndpointsForNodes(
    new Set(net.endpoints.map((id) => extractNodeRef('endpoint', id))),
  )
  activeExtractHighlightTexts = []
  activeExtractHighlightVectors = result.wires
    .filter((wire) => selectedIds.has(String(wire.id)))
    .flatMap((wire) => wire.vectors)
  const relatedComponentNodes = new Set<string>()
  for (const component of result.components) {
    if (!hasSharedEndpoint(net.endpoints, component.endpoints)) continue
    relatedComponentNodes.add(extractNodeRef('component', component.id))
  }
  relatedExtractHighlightVectors = extractVectorsForNodes(relatedComponentNodes)
  renderExtractVectorHighlights()
}

function highlightExtractedEndpoint(endpoint: ExtractedEndpoint): void {
  if (blockNonTraceHighlight()) return
  const relatedNodes = directlyRelatedExtractNodes(extractNodeRef('endpoint', endpoint.id))
  activeExtractHighlightEndpoints = [endpoint]
  activeExtractHighlightTexts = []
  activeExtractHighlightVectors = []
  relatedExtractHighlightVectors = extractVectorsForNodes(relatedNodes)
  renderExtractVectorHighlights()
}

function highlightExtractedRelation(relation: ExtractedRelation): void {
  if (blockNonTraceHighlight()) return
  const nodeRefs = new Set([String(relation.source ?? ''), String(relation.target ?? '')])
  activeExtractHighlightEndpoints = extractEndpointsForNodes(nodeRefs)
  activeExtractHighlightTexts = []
  activeExtractHighlightVectors = extractVectorsForNodes(nodeRefs)
  relatedExtractHighlightVectors = []
  renderExtractVectorHighlights()
}

function highlightExtractedHyperlink(hyperlink: ExtractedHyperlink): void {
  if (blockNonTraceHighlight()) return
  const component = hyperlink.source_component === null
    ? undefined
    : extractInfoResultData?.components.find((item) => (
        String(item.id) === String(hyperlink.source_component)
      ))
  if (component) {
    highlightExtractedItem('component', component.id, component.shape)
    if (hyperlink.source_bbox) {
      const { x0, y0, x1, y1 } = hyperlink.source_bbox
      activeExtractHighlightTexts = [{
        x0,
        y0,
        x1,
        y1,
        width: x1 - x0,
        height: y1 - y0,
      }]
    }
    renderExtractVectorHighlights()
    return
  }
  if (hyperlink.source_bbox) highlightRemainingText(hyperlink.source_bbox)
}

function highlightRemainingVector(vector: ApiPathBase): void {
  if (blockNonTraceHighlight()) return
  activeExtractHighlightVectors = [vector]
  relatedExtractHighlightVectors = []
  activeExtractHighlightEndpoints = []
  activeExtractHighlightTexts = []
  renderExtractVectorHighlights()
}

function highlightRemainingText(location: unknown): void {
  if (blockNonTraceHighlight()) return
  const coordinates = Array.isArray(location)
    ? location.slice(0, 4)
    : location && typeof location === 'object'
      ? [
          (location as Partial<BBox>).x0,
          (location as Partial<BBox>).y0,
          (location as Partial<BBox>).x1,
          (location as Partial<BBox>).y1,
        ]
      : []
  if (coordinates.length < 4) return
  const [x0, y0, x1, y1] = coordinates.map(Number)
  if (![x0, y0, x1, y1].every(Number.isFinite)) return
  activeExtractHighlightVectors = []
  relatedExtractHighlightVectors = []
  activeExtractHighlightEndpoints = []
  activeExtractHighlightTexts = [{
    x0,
    y0,
    x1,
    y1,
    width: x1 - x0,
    height: y1 - y0,
  }]
  renderExtractVectorHighlights()
}

function selectedVectorSourceIndex(item: VectorItem): number | null {
  const match = item.id.match(/-vector-(\d+)$/)
  if (!match) return null
  const oneBasedIndex = Number.parseInt(match[1], 10)
  return Number.isFinite(oneBasedIndex) && oneBasedIndex > 0 ? oneBasedIndex - 1 : null
}

function selectedTextSourceIndex(item: TextItem): number | null {
  const match = item.id.match(/-text-(\d+)$/)
  if (!match) return null
  const oneBasedIndex = Number.parseInt(match[1], 10)
  return Number.isFinite(oneBasedIndex) && oneBasedIndex > 0 ? oneBasedIndex - 1 : null
}

function extractedShapesContainPathIndex(
  shapes: ApiPathBase[],
  pathIndex: number,
  fallbackIndices: number[],
): boolean {
  const pathIndices = shapes
    .map((shape) => Number(shape.path_meta?.path_index))
    .filter(Number.isFinite)
  return pathIndices.length > 0
    ? pathIndices.includes(pathIndex)
    : fallbackIndices.includes(pathIndex)
}

function bboxIntersectionCoverage(left: BBox, right: BBox): number {
  const width = Math.max(0, Math.min(left.x1, right.x1) - Math.max(left.x0, right.x0))
  const height = Math.max(0, Math.min(left.y1, right.y1) - Math.max(left.y0, right.y0))
  const intersection = width * height
  const smallerArea = Math.min(
    Math.max(left.width * left.height, 0),
    Math.max(right.width * right.height, 0),
  )
  return smallerArea > 0 ? intersection / smallerArea : 0
}

function extractedCrosspageRelationForReaderLink(
  item: LinkItem,
  pageData: PageData,
): ExtractedHyperlink | undefined {
  const result = extractInfoResultData
  const targetPage = hyperlinkTargetPage(item)
  if (!result || result.page_number !== item.page_number || targetPage === null) return undefined
  const itemBox = mupdfBBoxToPdf(item.bbox, pageData.page_size.height_pt)
  return [
    ...(result.crosspage_relations.hyperlinks ?? []),
    ...(result.crosspage_relations.transfers ?? []),
  ]
    .filter((relation) => (
      relation.source_page === item.page_number
      && relation.target_page === targetPage
      && relation.source_bbox !== undefined
    ))
    .map((relation) => ({
      relation,
      score: bboxIntersectionCoverage(
        itemBox,
        normalizeApiBBox(relation.source_bbox!),
      ),
    }))
    .filter((candidate) => candidate.score > 0)
    .sort((left, right) => right.score - left.score)[0]?.relation
}

function extractedComponentBBox(component: ExtractedComponent): BBox | null {
  const value = component.bbox
  if (!value || typeof value !== 'object') return null
  const box = value as Partial<BBox>
  if (![box.x0, box.y0, box.x1, box.y1].every((coordinate) => Number.isFinite(Number(coordinate)))) {
    return null
  }
  return normalizeApiBBox(box as BBox)
}

function addComponentsRelatedToNode(nodeRef: string, componentIds: Set<string>): void {
  if (nodeRef.startsWith('component:')) {
    componentIds.add(nodeRef.slice('component:'.length))
    return
  }
  const firstHop = directlyRelatedExtractNodes(nodeRef)
  for (const related of firstHop) {
    if (related.startsWith('component:')) {
      componentIds.add(related.slice('component:'.length))
      continue
    }
    if (!related.startsWith('endpoint:')) continue
    for (const endpointRelated of directlyRelatedExtractNodes(related)) {
      if (endpointRelated.startsWith('component:')) {
        componentIds.add(endpointRelated.slice('component:'.length))
      }
    }
  }
}

function wireIdsForReaderItem(item: ReaderItem): Set<string> {
  const result = extractInfoResultData
  const wireIds = new Set<string>()
  if (
    !result
    || result.page_number !== item.page_number
    || item.kind !== 'vector_path'
  ) return wireIds
  const vectorIndex = selectedVectorSourceIndex(item)
  if (vectorIndex === null) return wireIds
  for (const wire of result.wires) {
    if (extractedShapesContainPathIndex(wire.vectors, vectorIndex, wire.vector_indices)) {
      wireIds.add(String(wire.id))
    }
  }
  return wireIds
}

function componentIdsForReaderItem(item: ReaderItem, pageData: PageData): Set<string> {
  const result = extractInfoResultData
  const componentIds = new Set<string>()
  if (!result || result.page_number !== item.page_number) return componentIds

  if (item.kind === 'vector_path') {
    const vectorIndex = selectedVectorSourceIndex(item)
    if (vectorIndex !== null) {
      for (const component of result.components) {
        if (extractedShapesContainPathIndex(component.shape, vectorIndex, component.vector_indices)) {
          componentIds.add(String(component.id))
        }
      }
    }
  } else if (item.kind === 'text') {
    const textIndex = selectedTextSourceIndex(item)
    if (textIndex !== null) {
      for (const ownership of extractTextOwnership) {
        if (!ownership.text_indices.includes(textIndex)) continue
        if (ownership.component_id !== undefined) {
          componentIds.add(String(ownership.component_id))
        } else if (ownership.endpoint_id !== undefined) {
          addComponentsRelatedToNode(
            extractNodeRef('endpoint', ownership.endpoint_id),
            componentIds,
          )
        }
      }
    }
  } else if (item.kind === 'link') {
    const itemBox = mupdfBBoxToPdf(item.bbox, pageData.page_size.height_pt)
    const matchedRelation = extractedCrosspageRelationForReaderLink(item, pageData)
    if (matchedRelation && matchedRelation.source_component !== null) {
      componentIds.add(String(matchedRelation.source_component))
    }
    // Persisted results produced while source_bbox was accidentally omitted
    // can still resolve a clicked PDF link by its source region.
    if (componentIds.size === 0) {
      const spatialCandidates = result.components
        .map((component) => {
          const bbox = extractedComponentBBox(component)
          return { component, score: bbox ? bboxIntersectionCoverage(itemBox, bbox) : 0 }
        })
        .filter((candidate) => candidate.score > 0)
        .sort((left, right) => right.score - left.score)
      if (spatialCandidates.length > 0) {
        componentIds.add(String(spatialCandidates[0].component.id))
      }
    }
  }

  if (componentIds.size > 0 || item.kind === 'vector_path' || item.kind === 'link') {
    return componentIds
  }

  const itemBox = mupdfBBoxToPdf(item.bbox, pageData.page_size.height_pt)
  const spatialCandidates = result.components
    .map((component) => {
      const bbox = extractedComponentBBox(component)
      return { component, score: bbox ? bboxIntersectionCoverage(itemBox, bbox) : 0 }
    })
    .filter((candidate) => candidate.score > 0)
    .sort((left, right) => right.score - left.score)
  if (spatialCandidates.length > 0) {
    componentIds.add(String(spatialCandidates[0].component.id))
  }
  return componentIds
}

function openExtractJsonPath(node: HTMLDetailsElement): void {
  let current: HTMLElement | null = node
  while (current && current !== extractInfoResult) {
    if (current instanceof HTMLDetailsElement) current.open = true
    current = current.parentElement
  }
}

function focusExtractComponentById(componentId: number | string): boolean {
  if (blockNonTraceHighlight()) return false
  const result = extractInfoResultData
  const id = String(componentId)
  if (!result || result.page_number !== activePageNumber) return false
  const component = result.components.find((item) => String(item.id) === id)
  const target = [
    ...extractInfoResult.querySelectorAll<HTMLDetailsElement>(
      'details[data-extract-category="components"][data-extract-id]',
    ),
  ].find((node) => node.dataset.extractId === id)
  if (!component || !target) return false

  setInspectorTab('extract-info')
  extractInfoResult.querySelectorAll('.is-search-selected')
    .forEach((node) => node.classList.remove('is-search-selected'))
  target.classList.add('is-search-selected')
  openExtractJsonPath(target)
  highlightExtractedItem('component', component.id, component.shape)
  extractJsonSearchStatus.textContent = `Selected target component ${id}.`
  window.requestAnimationFrame(() => {
    target.scrollIntoView({ behavior: 'smooth', block: 'center' })
  })
  return true
}

function focusExtractInfoForReaderItem(item: ReaderItem, pageData: PageData): boolean {
  if (blockNonTraceHighlight()) return false
  const result = extractInfoResultData
  if (!result || result.page_number !== item.page_number) return false
  const componentIds = componentIdsForReaderItem(item, pageData)
  const wireIds = wireIdsForReaderItem(item)
  if (componentIds.size === 0 && wireIds.size === 0) return false

  const allDetails = [
    ...extractInfoResult.querySelectorAll<HTMLDetailsElement>('details'),
  ]
  for (const details of allDetails) {
    details.open = false
    details.classList.remove('is-search-selected')
  }

  const selectedNodes: HTMLDetailsElement[] = []
  const objectNodes = [
    ...extractInfoResult.querySelectorAll<HTMLDetailsElement>(
      'details[data-extract-category][data-extract-id]',
    ),
  ]
  for (const node of objectNodes) {
    const category = node.dataset.extractCategory
    const id = node.dataset.extractId ?? ''
    if (
      (category === 'components' && componentIds.has(id))
      || (category === 'wires' && wireIds.has(id))
    ) {
      selectedNodes.push(node)
    }
  }

  for (const node of selectedNodes) {
    node.classList.add('is-search-selected')
    openExtractJsonPath(node)
  }
  const selectedComponents = result.components.filter((component) => (
    componentIds.has(String(component.id))
  ))
  const selectedWires = result.wires.filter((wire) => wireIds.has(String(wire.id)))
  activeExtractHighlightVectors = [
    ...selectedComponents.flatMap((component) => component.shape),
    ...selectedWires.flatMap((wire) => wire.vectors),
  ]
  const primaryRefs = new Set([
    ...[...componentIds].map((componentId) => extractNodeRef('component', componentId)),
    ...[...wireIds].map((wireId) => extractNodeRef('wire', wireId)),
  ])
  const { endpointRefs, relatedRefs } = endpointLinkedExtractNodes(primaryRefs)
  relatedExtractHighlightVectors = extractVectorsForNodes(relatedRefs)
  activeExtractHighlightEndpoints = extractEndpointsForNodes(endpointRefs)
  activeExtractHighlightTexts = []
  renderExtractVectorHighlights()
  selectedNodes[0]?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  const focusedLabels = []
  if (componentIds.size > 0) {
    focusedLabels.push(`component${componentIds.size === 1 ? '' : 's'} ${[...componentIds].join(', ')}`)
  }
  if (wireIds.size > 0) {
    focusedLabels.push(`wire${wireIds.size === 1 ? '' : 's'} ${[...wireIds].join(', ')}`)
  }
  extractJsonSearchStatus.textContent = `Focused ${focusedLabels.join(' and ')}.`
  return true
}

function searchSelectedVectorOwnership(): void {
  if (isInfoTraceActive()) {
    vectorOwnerResult.textContent = 'Stop Info Trace before using vector ownership highlights.'
    renderInfoTraceHighlights()
    return
  }
  const selected = activeSelectionId
    ? currentPageState?.pageData.items.find((item) => item.id === activeSelectionId)
    : null
  if (!selected || selected.kind !== 'vector_path') {
    vectorOwnerResult.textContent = 'Select one vector in the PDF viewer before searching.'
    return
  }
  const result = extractInfoResultData
  if (!result || result.page_number !== selected.page_number) {
    vectorOwnerResult.textContent = 'No extraction result is available for this page. Run Extract Info first.'
    return
  }
  const vectorIndex = selectedVectorSourceIndex(selected)
  if (vectorIndex === null) {
    vectorOwnerResult.textContent = 'Unable to determine this vector’s source index.'
    return
  }

  const components = result.components.filter((component) => (
    extractedShapesContainPathIndex(component.shape, vectorIndex, component.vector_indices)
  ))
  const wires = result.wires.filter((wire) => (
    extractedShapesContainPathIndex(wire.vectors, vectorIndex, wire.vector_indices)
  ))
  const endpoints = (result.endpoints ?? []).filter((endpoint) => (
    extractedShapesContainPathIndex(endpoint.shape, vectorIndex, endpoint.vector_indices ?? [])
  ))
  if (!components.length && !wires.length && !endpoints.length) {
    vectorOwnerResult.innerHTML = `<div class="vector-owner-empty">Vector index ${vectorIndex} does not belong to any extracted Component, Wire, or Endpoint.</div>`
    clearExtractVectorHighlights()
    return
  }

  const rows: string[] = []
  for (const component of components) {
    rows.push(
      `<div class="vector-owner-card"><strong>Component ${escapeHtml(String(component.id))}</strong>`
      + `<span>Elements ${component.elements.map((id) => escapeHtml(String(id))).join(', ')}</span></div>`,
    )
  }
  for (const wire of wires) {
    const net = (result.nets ?? []).find((candidate) => (
      candidate.wire_ids.some((id) => String(id) === String(wire.id))
    ))
    rows.push(
      `<div class="vector-owner-card"><strong>Wire ${escapeHtml(String(wire.id))}</strong>`
      + `<span>Net ${net ? escapeHtml(String(net.id)) : 'None'}</span></div>`,
    )
  }
  for (const endpoint of endpoints) {
    const endpointComponents = result.components
      .filter((component) => component.endpoints.some((id) => String(id) === String(endpoint.id)))
    const nets = (result.nets ?? [])
      .filter((net) => net.endpoints.some((id) => String(id) === String(endpoint.id)))
    rows.push(
      `<div class="vector-owner-card"><strong>Endpoint ${escapeHtml(String(endpoint.id))}</strong>`
      + `<span>Components ${endpointComponents.length ? endpointComponents.map((component) => escapeHtml(String(component.id))).join(', ') : 'None'}`
      + ` · Nets ${nets.length ? nets.map((net) => escapeHtml(String(net.id))).join(', ') : 'None'}</span></div>`,
    )
  }
  vectorOwnerResult.innerHTML = `<div class="vector-owner-index">Vector index ${vectorIndex}</div>${rows.join('')}`
  activeExtractHighlightVectors = [
    ...components.flatMap((component) => component.shape),
    ...wires.flatMap((wire) => wire.vectors),
  ]
  relatedExtractHighlightVectors = []
  activeExtractHighlightEndpoints = endpoints
  activeExtractHighlightTexts = []
  renderExtractVectorHighlights()
}

function jsonScalar(value: unknown): string {
  if (typeof value === 'string') {
    if (value.startsWith('data:image/')) return '"[embedded component image]"'
    return JSON.stringify(value)
  }
  if (value === null) return 'null'
  return String(value)
}

function buildJsonNode(
  value: unknown,
  label: string,
  initiallyOpen = false,
  collection?: 'remaining_vectors' | 'remaining_text' | 'hyperlinks' | 'transfers',
): HTMLElement {
  if (value === null || typeof value !== 'object') {
    const row = document.createElement('div')
    row.className = 'json-scalar'
    row.innerHTML = `<span class="json-key">${escapeHtml(label)}</span>: <span>${escapeHtml(jsonScalar(value))}</span>`
    return row
  }

  const details = document.createElement('details')
  details.className = 'json-branch'
  details.open = initiallyOpen
  const summary = document.createElement('summary')
  const size = Array.isArray(value) ? value.length : Object.keys(value as object).length
  summary.textContent = `${label} ${Array.isArray(value) ? `[${size}]` : `{${size}}`}`
  details.appendChild(summary)
  const body = document.createElement('div')
  body.className = 'json-children'

  const record = value as Record<string, unknown>
  const isRemainingVector =
    collection === 'remaining_vectors'
    && Array.isArray(record.points)
    && record.bbox !== null
    && typeof record.bbox === 'object'
  const isRemainingText =
    collection === 'remaining_text'
    && record.bbox !== null
    && typeof record.bbox === 'object'
    && typeof record.text === 'string'
  const isEndpoint = record.type === 'endpoint' && Array.isArray(record.elements)
  const isGroup = record.type === 'group' && Array.isArray(record.elements)
  const isComponent = record.type === 'component' && Array.isArray(record.elements)
  const isWire = record.type === 'wire' && Array.isArray(record.elements)
  const isNet = record.type === 'net' && Array.isArray(record.elements)
  const isElement = !isEndpoint && !isGroup && !isComponent && !isWire && !isNet
    && Array.isArray(record.shape) && Array.isArray(record.vector_indices)
  const isRelation =
    typeof record.type === 'string'
    && typeof record.source === 'string'
    && typeof record.target === 'string'
  const isHyperlink =
    typeof record.source_page === 'number'
    && 'source_component' in record
    && typeof record.target_page === 'number'
  if (isComponent) {
    details.dataset.extractCategory = 'components'
    details.dataset.extractId = String(record.id)
  } else if (isElement) {
    details.dataset.extractCategory = 'elements'
    details.dataset.extractId = String(record.id)
  } else if (isGroup) {
    details.dataset.extractCategory = 'groups'
    details.dataset.extractId = String(record.id)
  } else if (isWire) {
    details.dataset.extractCategory = 'wires'
    details.dataset.extractId = String(record.id)
  } else if (isEndpoint) {
    details.dataset.extractCategory = 'endpoints'
    details.dataset.extractId = String(record.id)
  } else if (isNet) {
    details.dataset.extractCategory = 'nets'
    details.dataset.extractId = String(record.id)
  } else if (isRelation) {
    details.dataset.extractCategory = 'relations'
    details.dataset.relationSource = String(record.source)
    details.dataset.relationTarget = String(record.target)
  }
  if (isRemainingVector) {
    details.classList.add('extract-json-item', 'extract-json-remaining')
    summary.addEventListener('click', (event) => {
      event.stopPropagation()
      highlightRemainingVector(record as unknown as ApiPathBase)
    })
  } else if (isRemainingText) {
    details.classList.add('extract-json-item', 'extract-json-remaining')
    summary.addEventListener('click', (event) => {
      event.stopPropagation()
      highlightRemainingText(record.bbox)
    })
  } else if (isComponent || isElement || isGroup || isWire) {
    details.classList.add('extract-json-item')
    summary.addEventListener('click', (event) => {
      event.stopPropagation()
      const vectors = (isComponent || isElement || isGroup ? record.shape : record.vectors) as ApiPathBase[]
      highlightExtractedItem(
        isComponent ? 'component' : isElement ? 'element' : isGroup ? 'group' : 'wire',
        record.id,
        vectors,
      )
    })
    if (isComponent && typeof record.image === 'string') {
      const image = document.createElement('img')
      image.className = 'extract-component-image'
      image.src = record.image
      image.alt = `Component ${String(record.id ?? '')}`
      image.addEventListener('click', () => {
        highlightExtractedItem(
          'component',
          record.id,
          record.shape as ApiPathBase[],
        )
      })
      body.appendChild(image)
    }
  } else if (isEndpoint) {
    details.classList.add('extract-json-item', 'extract-json-endpoint')
    summary.addEventListener('click', (event) => {
      event.stopPropagation()
      highlightExtractedEndpoint(record as ExtractedEndpoint)
    })
  } else if (isRelation) {
    details.classList.add('extract-json-item', 'extract-json-relation')
    summary.addEventListener('click', (event) => {
      event.stopPropagation()
      highlightExtractedRelation(record as ExtractedRelation)
    })
  } else if (isHyperlink) {
    details.classList.add('extract-json-item', 'extract-json-hyperlink')
    summary.addEventListener('click', (event) => {
      event.stopPropagation()
      highlightExtractedHyperlink(record as ExtractedHyperlink)
    })
  } else if (isNet) {
    details.classList.add('extract-json-item')
    summary.addEventListener('click', (event) => {
      event.stopPropagation()
      highlightExtractedNet(record as ExtractedNet)
    })
  }

  const entries = Array.isArray(value)
    ? value.map((item, index) => [String(index), item] as const)
    : Object.entries(record).filter(([key]) => key !== 'image')
  for (const [key, item] of entries) {
    let childLabel = key
    if (Array.isArray(value)) {
      const itemRecord = item && typeof item === 'object' ? item as Record<string, unknown> : null
      if (collection === 'remaining_vectors' && itemRecord) {
        const pathMeta = itemRecord.path_meta && typeof itemRecord.path_meta === 'object'
          ? itemRecord.path_meta as Record<string, unknown>
          : null
        childLabel = `remaining vector ${String(pathMeta?.path_index ?? key)}`
      } else if (collection === 'remaining_text' && itemRecord) {
        childLabel = `remaining text ${String(itemRecord.index ?? key)} · ${String(itemRecord.text ?? '')}`
      } else if (itemRecord && itemRecord.type === 'endpoint') {
        childLabel = `endpoint ${String(itemRecord.id ?? key)}`
      } else if (itemRecord && itemRecord.type === 'component') {
        childLabel = `component ${String(itemRecord.id ?? key)}`
      } else if (itemRecord && itemRecord.type === 'group') {
        childLabel = `group ${String(itemRecord.id ?? key)}`
      } else if (itemRecord && itemRecord.type === 'wire') {
        childLabel = `wire ${String(itemRecord.id ?? key)}`
      } else if (itemRecord && itemRecord.type === 'net') {
        childLabel = `net ${String(itemRecord.id ?? key)}`
      } else if (itemRecord && Array.isArray(itemRecord.shape)) {
        childLabel = `element ${String(itemRecord.id ?? key)}`
      } else if (
        itemRecord
        && typeof itemRecord.source_page === 'number'
        && typeof itemRecord.target_page === 'number'
        && 'source_component' in itemRecord
      ) {
        const componentLabel = itemRecord.source_component === null
          ? 'unresolved component'
          : `component ${String(itemRecord.source_component)}`
        const linkType = collection === 'transfers' ? 'transfer' : 'hyperlink'
        childLabel = `${linkType} page ${itemRecord.source_page} -> ${itemRecord.target_page} · ${componentLabel}`
      } else if (
        itemRecord
        && typeof itemRecord.source === 'string'
        && typeof itemRecord.target === 'string'
      ) {
        childLabel = `${String(itemRecord.type ?? 'relation')} ${itemRecord.source} → ${itemRecord.target}`
      } else {
        childLabel = `item ${key}`
      }
    }
    const childCollection = Array.isArray(value)
      ? collection
      : key === 'remaining_vectors'
        || key === 'remaining_text'
        || key === 'hyperlinks'
        || key === 'transfers'
        ? key
        : undefined
    body.appendChild(buildJsonNode(
      item,
      childLabel,
      false,
      childCollection,
    ))
  }
  details.appendChild(body)
  return details
}

function renderExtractInfoResult(result: ExtractInfoResult): void {
  extractInfoResult.replaceChildren()
  extractJsonSearchStatus.textContent = ''
  const summary = document.createElement('div')
  summary.className = 'extract-result-summary'
  summary.textContent = `Page ${result.page_number} · ${result.elements.length} elements · ${result.components.length} components · ${result.wires.length} wires · ${result.endpoints.length} endpoints · ${result.nets.length} nets · ${result.groups.length} groups · ${result.crosspage_relations.hyperlinks.length} hyperlinks · ${result.crosspage_relations.transfers.length} transfers · ${result.info_table?.cells.length ?? 0} info cells`
  const elementsByType = Object.fromEntries(
    Object.entries(
      result.elements.reduce<Record<string, ExtractedElement[]>>((groups, element) => {
        const type = String(element.type || 'unknown')
        ;(groups[type] ??= []).push(element)
        return groups
      }, {}),
    ).sort(([left], [right]) => left.localeCompare(right)),
  )
  const visualResult = {
    diagram: {
      ...result.diagram,
      elements: elementsByType,
    },
    info_table: result.info_table,
    crosspage_relations: result.crosspage_relations,
  }
  const jsonTree = buildJsonNode(visualResult, 'result', true)
  extractInfoResult.append(summary, jsonTree)
}

function searchExtractInfoJson(): void {
  if (isInfoTraceActive()) {
    extractJsonSearchStatus.textContent = 'Stop Info Trace before using JSON search highlights.'
    renderInfoTraceHighlights()
    return
  }
  const result = extractInfoResultData
  const category = extractJsonSearchCategory.value
  const id = extractJsonSearchId.value.trim()
  if (!result) {
    extractJsonSearchStatus.textContent = 'Run Extract Info first.'
    return
  }
  if (!id) {
    extractJsonSearchStatus.textContent = 'Enter an ID.'
    return
  }
  const target = [...extractInfoResult.querySelectorAll<HTMLDetailsElement>('details[data-extract-category]')]
    .find((node) => node.dataset.extractCategory === category && node.dataset.extractId === id)
  extractInfoResult.querySelectorAll('.is-search-selected')
    .forEach((node) => node.classList.remove('is-search-selected'))
  if (!target) {
    extractJsonSearchStatus.textContent = `No ${category.slice(0, -1)} with ID ${id}.`
    return
  }
  let ancestor: HTMLElement | null = target
  while (ancestor && ancestor !== extractInfoResult) {
    if (ancestor instanceof HTMLDetailsElement) ancestor.open = true
    ancestor = ancestor.parentElement
  }
  target.classList.add('is-search-selected')
  target.scrollIntoView({ behavior: 'smooth', block: 'center' })
  extractJsonSearchStatus.textContent = `Selected ${category.slice(0, -1)} ${id}.`
  const numericOrStringId = /^-?\d+$/.test(id) ? Number.parseInt(id, 10) : id
  if (category === 'components') {
    const component = result.components.find((item) => String(item.id) === id)
    if (component) highlightExtractedItem('component', numericOrStringId, component.shape)
  } else if (category === 'wires') {
    const wire = result.wires.find((item) => String(item.id) === id)
    if (wire) highlightExtractedItem('wire', numericOrStringId, wire.vectors)
  } else if (category === 'endpoints') {
    const endpoint = result.endpoints?.find((item) => String(item.id) === id)
    if (endpoint) highlightExtractedEndpoint(endpoint)
  } else if (category === 'nets') {
    const net = result.nets?.find((item) => String(item.id) === id)
    if (net) highlightExtractedNet(net)
  } else if (category === 'elements') {
    const element = result.elements.find((item) => String(item.id) === id)
    if (element) highlightExtractedItem('element', numericOrStringId, element.shape)
  } else if (category === 'groups') {
    const group = result.groups.find((item) => String(item.id) === id)
    if (group) highlightExtractedItem('group', numericOrStringId, group.shape)
  }
}

function updateExtractProgress(progress: number, message: string): void {
  extractProgressBar.style.width = `${Math.max(0, Math.min(progress, 100))}%`
  extractInfoStatus.textContent = `${progress}% · ${message}`
}

const LLM_GENERATE_MAX_ATTEMPTS = 3

class GenerateRequestError extends Error {
  readonly retryable: boolean

  constructor(message: string, retryable = true) {
    super(message)
    this.name = 'GenerateRequestError'
    this.retryable = retryable
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

async function responseErrorReason(response: Response): Promise<string> {
  const fallback = `HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ''}`
  let body = ''
  try {
    body = await response.text()
  } catch {
    return fallback
  }
  if (!body.trim()) return fallback
  try {
    const payload = JSON.parse(body) as { error?: unknown; message?: unknown }
    const reason = payload.error ?? payload.message
    return reason ? String(reason) : fallback
  } catch {
    return body.trim()
  }
}

function isRetryableHttpStatus(status: number): boolean {
  return status === 408 || status === 409 || status === 425 || status === 429 || status >= 500
}

function waitForRetry(delayMs: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, delayMs)
    const onAbort = () => {
      window.clearTimeout(timeout)
      reject(new DOMException('Extraction aborted', 'AbortError'))
    }
    if (signal.aborted) onAbort()
    else signal.addEventListener('abort', onAbort, { once: true })
  })
}

async function generateExtractInfo(
  requestBody: Record<string, unknown>,
  signal: AbortSignal,
): Promise<ExtractInfoResult> {
  const response = await fetch('/api/vector-matcher', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    signal,
    body: JSON.stringify(requestBody),
  })
  if (!response.ok) {
    const reason = await responseErrorReason(response)
    throw new GenerateRequestError(reason, isRetryableHttpStatus(response.status))
  }
  if (!response.body) throw new GenerateRequestError('LLM generate response has no body')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let finalResult: ExtractInfoResult | null = null

  const processLine = (line: string): void => {
    if (!line.trim()) return
    let message: {
      event?: string
      progress?: number
      message?: string
      ok?: boolean
      error?: string
      result?: ExtractInfoResult
    }
    try {
      message = JSON.parse(line) as typeof message
    } catch {
      throw new GenerateRequestError(`Invalid LLM API response: ${line.trim()}`)
    }
    if (message.event === 'progress') {
      updateExtractProgress(message.progress ?? 0, message.message ?? 'Processing')
    } else if (message.ok && message.result) {
      finalResult = message.result
    } else if (message.ok === false) {
      throw new GenerateRequestError(message.error || 'LLM generate failed')
    }
  }

  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value, { stream: !done })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) processLine(line)
    if (done) break
  }
  processLine(buffer)
  if (!finalResult) throw new GenerateRequestError('LLM generate finished without a result')
  return finalResult
}

async function restorePersistedState(pageNumber: number): Promise<void> {
  const documentAtRequest = activeDocument
  if (!documentAtRequest) return
  extractInfoResult.innerHTML = `
    <div class="extract-info-loading">
      <div class="viewer-loading-spinner"></div>
      <span>Loading saved extraction result...</span>
    </div>
  `
  extractInfoStatus.textContent = 'Loading saved extraction result...'
  try {
    const response = await fetch('/api/vector-matcher', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mode: 'persisted_state',
        pdf_url: documentAtRequest.pdf_url,
        page: pageNumber,
      }),
    })
    const payload = (await response.json()) as {
      ok: boolean
      error?: string
      result?: {
        symbols?: SymbolExtractResult | null
        extract_info?: ExtractInfoResult | null
        extract_info_source?: 'document_result' | 'page_cache' | null
        document_result_filename?: string | null
      }
    }
    if (activeDocument !== documentAtRequest || activePageNumber !== pageNumber) return
    if (!response.ok || !payload.ok || !payload.result) {
      extractInfoResult.replaceChildren()
      extractInfoRunBtn.textContent = 'Extract'
      updateExtractProgress(0, 'No saved Extract Info for this page')
      return
    }

    if (payload.result.symbols) {
      lastExtractedPages = payload.result.symbols.pages
      symbolPagesInput.value = lastExtractedPages.join(', ')
      hasSymbolExtractionResult = true
      renderSymbolResult(payload.result.symbols)
      symbolExtractBtn.textContent = 'Regenerate symbols'
      setSymbolStatus(`Restored saved Symbols result from page${lastExtractedPages.length === 1 ? '' : 's'} ${lastExtractedPages.join(', ')}.`)
      updateExtractInfoAvailability()
    }
    clearExtractVectorHighlights()
    extractInfoResultData = payload.result.extract_info
      ? hydrateExtractInfoResult(payload.result.extract_info)
      : null
    if (!extractInfoResultData) extractTextOwnership = []
    extractInfoResult.replaceChildren()
    if (extractInfoResultData) {
      renderExtractInfoResult(extractInfoResultData)
      extractInfoRunBtn.textContent = 'Regenerate'
      updateExtractProgress(
        100,
        payload.result.extract_info_source === 'document_result'
          ? `Loaded ${payload.result.document_result_filename ?? 'document parsing result'}`
          : 'Loaded saved Extract Info',
      )
    } else {
      extractInfoRunBtn.textContent = 'Extract'
      updateExtractProgress(0, 'No saved Extract Info for this page')
    }
    renderInfoTraceHighlights()
  } catch {
    // Persistence is an optimization; the normal extraction controls remain usable.
    if (activeDocument === documentAtRequest && activePageNumber === pageNumber) {
      extractInfoResult.replaceChildren()
      updateExtractProgress(0, 'Failed to load saved Extract Info')
    }
  }
}

async function runExtractInfo(): Promise<void> {
  if (!activeDocument || !currentPageState) {
    extractInfoStatus.textContent = 'Open a PDF page before extracting.'
    return
  }
  const requestedPage = activePageNumber
  if (!hasSymbolExtractionResult || !lastExtractedPages.length) {
    extractInfoStatus.textContent = 'Run extraction in the Symbols tab first.'
    updateExtractInfoAvailability()
    return
  }
  isExtractInfoRunning = true
  extractInfoCancellationRequested = false
  extractInfoAbortController = new AbortController()
  updateExtractInfoAvailability()
  extractInfoResult.replaceChildren()
  clearExtractVectorHighlights()
  updateExtractProgress(0, `Starting page ${requestedPage}`)
  try {
    let finalResult: ExtractInfoResult | null = null
    const failures: string[] = []
    const maxAttempts = extractLlmGateInput.checked ? LLM_GENERATE_MAX_ATTEMPTS : 1
    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      try {
        finalResult = await generateExtractInfo({
          mode: 'extract_info',
          pdf_url: activeDocument.pdf_url,
          page: requestedPage,
          pages: lastExtractedPages,
          use_llm_gate: extractLlmGateInput.checked,
          force_regenerate: true,
        }, extractInfoAbortController.signal)
        break
      } catch (error) {
        if (extractInfoAbortController.signal.aborted) throw error
        const reason = errorMessage(error)
        failures.push(reason)
        const retryable = !(error instanceof GenerateRequestError) || error.retryable
        if (!retryable || attempt === maxAttempts) {
          const reasons = [...new Set(failures)].join('; ')
          throw new Error(
            failures.length > 1
              ? `LLM generate failed after ${failures.length} attempts: ${reasons}`
              : reason,
          )
        }
        updateExtractProgress(0, `LLM API error (${attempt}/${maxAttempts}): ${reason}. Retrying...`)
        await waitForRetry(500 * attempt, extractInfoAbortController.signal)
      }
    }
    if (!finalResult) throw new Error('Extraction finished without a result')
    if (activePageNumber !== requestedPage) {
      throw new Error(`Page changed while extracting; result belongs to page ${requestedPage}`)
    }
    finalResult = hydrateExtractInfoResult(finalResult)
    extractInfoResultData = finalResult
    renderExtractInfoResult(finalResult)
    extractInfoRunBtn.textContent = 'Regenerate'
    updateExtractProgress(100, 'Extraction complete')
    renderInfoTraceHighlights()
  } catch (error) {
    extractInfoResultData = null
    extractTextOwnership = []
    const message = error instanceof Error ? error.message : String(error)
    if (extractInfoCancellationRequested || (error instanceof DOMException && error.name === 'AbortError')) {
      extractInfoStatus.textContent = 'Extraction stopped. Run Symbols extraction again before retrying.'
    } else if (message.includes('Symbol extraction result is unavailable')) {
      hasSymbolExtractionResult = false
      lastExtractedPages = []
      lastExtractedSymbolCount = 0
      updateExtractInfoAvailability()
      extractInfoStatus.textContent = `Extraction failed: ${message}`
    } else {
      extractInfoStatus.textContent = `Extraction failed: ${message}`
    }
  } finally {
    isExtractInfoRunning = false
    extractInfoAbortController = null
    updateExtractInfoAvailability()
  }
}

async function cancelExtractInfo(): Promise<void> {
  if (!isExtractInfoRunning || extractInfoCancellationRequested) return
  extractInfoCancellationRequested = true
  updateExtractInfoAvailability()
  extractInfoStatus.textContent = 'Stopping extraction...'
  try {
    await fetch('/api/vector-matcher', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'cancel_extract_info' }),
    })
  } catch {
    // The original request is still aborted below even if the acknowledgement is lost.
  } finally {
    extractInfoAbortController?.abort()
    hasSymbolExtractionResult = false
    lastExtractedPages = []
    lastExtractedSymbolCount = 0
    extractInfoResultData = null
    extractTextOwnership = []
    clearExtractVectorHighlights()
    extractInfoResult.replaceChildren()
    extractInfoStatus.textContent = 'Extraction stopped. Run Symbols extraction again before retrying.'
    updateExtractInfoAvailability()
  }
}

function getSymbolSearchCacheKey(group: VectorEntityGroup, pages: number[]): string {
  return `${group.root}:${pages.join(',')}`
}

function applySymbolSearchResult(result: SymbolSearchResult): number {
  // Keep matches found for other entities/pages. Re-running the same entity
  // replaces only that entity's previous result instead of duplicating boxes.
  symbolMatchBoxes = symbolMatchBoxes.filter(
    (box) => box.pageNumber !== result.page || box.entityRoot !== result.entity_root,
  )
  symbolMatchBoxes.push(...result.matches.flatMap((match) =>
    match.boxes.map((bbox) => ({
      symbol: match.symbol,
      pageNumber: result.page,
      entityRoot: result.entity_root,
      bbox,
    })),
  ))
  activeSymbolHighlight = null
  renderSymbolMatchOverlays()
  syncSymbolCardHighlight()
  return result.matches.length
}

function setSymbolSearchResultStatus(result: SymbolSearchResult, entityIndex: number, cached = false): void {
  const matchedSymbols = result.matches.length
  const matchCount = result.matches.reduce((total, match) => total + match.boxes.length, 0)
  const cachePrefix = cached ? 'Restored cached result: ' : ''
  setSymbolStatus(
    matchCount
      ? `${cachePrefix}Found ${matchCount} match${matchCount === 1 ? '' : 'es'} ` +
          `for ${matchedSymbols} symbol${matchedSymbols === 1 ? '' : 's'} inside entity ${entityIndex + 1} ` +
          `on page ${result.page}. ` +
          `Click a symbol to highlight its matches.`
      : `${cachePrefix}No symbol matches found inside entity ${entityIndex + 1} on page ${result.page}.`,
  )
}

// Build a normalized SVG thumbnail. Symbol points arrive in MuPDF space
// (top-left origin, y-down), already translated to a local origin, so they map
// straight onto the SVG coordinate system without a y-flip.
function buildSymbolPreview(symbol: ApiSymbol): SVGSVGElement {
  const SIZE = 64
  const PAD = 5
  const svg = document.createElementNS(SVG_NS, 'svg')
  svg.setAttribute('class', 'symbol-preview')
  svg.setAttribute('viewBox', `0 0 ${SIZE} ${SIZE}`)
  const span = Math.max(symbol.width, symbol.height, 1e-6)
  const scale = (SIZE - PAD * 2) / span
  const offsetX = PAD + (SIZE - PAD * 2 - symbol.width * scale) / 2
  const offsetY = PAD + (SIZE - PAD * 2 - symbol.height * scale) / 2
  const tx = (x: number): number => offsetX + x * scale
  const ty = (y: number): number => offsetY + y * scale
  for (const shape of symbol.shapes) {
    const pts = shape.points
    if (!pts || pts.length < 2) continue
    let d: string
    if (shape.type === 'curve' && pts.length >= 4) {
      d = `M ${tx(pts[0][0])} ${ty(pts[0][1])} C ${tx(pts[1][0])} ${ty(pts[1][1])}, ${tx(pts[2][0])} ${ty(pts[2][1])}, ${tx(pts[3][0])} ${ty(pts[3][1])}`
    } else {
      d = `M ${tx(pts[0][0])} ${ty(pts[0][1])}`
      for (let i = 1; i < pts.length; i += 1) {
        d += ` L ${tx(pts[i][0])} ${ty(pts[i][1])}`
      }
      if (shape.type === 'rect' || shape.type === 'quad') d += ' Z'
    }
    const path = document.createElementNS(SVG_NS, 'path')
    path.setAttribute('class', shape.dashed ? 'symbol-preview-path is-dashed' : 'symbol-preview-path')
    path.setAttribute('d', d)
    svg.appendChild(path)
  }
  return svg
}

function renderSymbolResult(result: SymbolExtractResult): void {
  symbolList.replaceChildren()
  lastExtractedSymbolCount = result.records.length
  updateSymbolSearchButton()
  const pageLabel = `page${result.pages.length === 1 ? '' : 's'} ${result.pages.join(', ')}`
  if (!result.records.length) {
    setSymbolStatus(`No symbols found on ${pageLabel}.`)
    return
  }
  setSymbolStatus(
    `Extracted ${result.records.length} component${result.records.length === 1 ? '' : 's'} ` +
      `(${result.symbols.length} unique shape${result.symbols.length === 1 ? '' : 's'}) from ${pageLabel}.`,
  )
  for (const record of result.records) {
    const symbol = result.symbols[record.symbol]
    const card = document.createElement('div')
    card.className = 'symbol-card'
    card.dataset.symbol = String(record.symbol)

    const header = document.createElement('button')
    header.type = 'button'
    header.className = 'symbol-card-header'
    header.setAttribute('aria-expanded', 'false')

    const preview = document.createElement('div')
    preview.className = 'symbol-card-preview'
    if (symbol) preview.appendChild(buildSymbolPreview(symbol))
    header.appendChild(preview)

    const name = document.createElement('span')
    name.className = 'symbol-card-name'
    name.textContent = record.name || '(unnamed)'
    header.appendChild(name)

    const chevron = document.createElement('span')
    chevron.className = 'symbol-card-chevron'
    chevron.setAttribute('aria-hidden', 'true')
    chevron.textContent = '▸'
    header.appendChild(chevron)

    const body = document.createElement('div')
    body.className = 'symbol-card-body'
    body.hidden = true
    body.textContent = record.description || 'No description.'

    header.addEventListener('click', () => {
      const expanded = header.getAttribute('aria-expanded') === 'true'
      header.setAttribute('aria-expanded', String(!expanded))
      body.hidden = expanded
      card.classList.toggle('is-open', !expanded)
      setActiveSymbolHighlight(record.symbol)
    })

    card.appendChild(header)
    card.appendChild(body)
    symbolList.appendChild(card)
  }
}

async function runSymbolExtraction(): Promise<void> {
  if (!activeDocument) {
    setSymbolStatus('Open a PDF before extracting symbols.')
    return
  }
  const pages = parseSymbolPages(symbolPagesInput.value)
  if (!pages.length) {
    setSymbolStatus('Enter at least one valid page number, for example 5, 6.')
    return
  }
  symbolPagesInput.value = pages.join(', ')
  symbolExtractBtn.disabled = true
  hasSymbolExtractionResult = false
  lastExtractedPages = []
  lastExtractedSymbolCount = 0
  updateExtractInfoAvailability()
  setSymbolStatus(`Extracting symbols from page${pages.length === 1 ? '' : 's'} ${pages.join(', ')}...`)
  try {
    const response = await fetch('/api/vector-matcher', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mode: 'symbols',
        pdf_url: activeDocument.pdf_url,
        page: pages[0],
        pages,
        force_regenerate: true,
      }),
    })
    const payload = (await response.json()) as {
      ok: boolean
      error?: string
      result?: SymbolExtractResult
    }
    if (!response.ok || !payload.ok || !payload.result) {
      throw new Error(payload.error || `Symbol extraction failed: ${response.status}`)
    }
    lastExtractedPages = payload.result.pages
    hasSymbolExtractionResult = true
    clearSymbolMatches()
    renderSymbolResult(payload.result)
    symbolExtractBtn.textContent = 'Regenerate symbols'
    updateExtractInfoAvailability()
  } catch (error) {
    symbolList.replaceChildren()
    lastExtractedPages = []
    lastExtractedSymbolCount = 0
    hasSymbolExtractionResult = false
    updateSymbolSearchButton()
    updateExtractInfoAvailability()
    clearSymbolMatches()
    setSymbolStatus(`Symbol extraction failed: ${error instanceof Error ? error.message : String(error)}`)
  } finally {
    symbolExtractBtn.disabled = false
  }
}

function clearSymbolMatches(): void {
  symbolMatchBoxes = []
  activeSymbolHighlight = null
  renderSymbolMatchOverlays()
  syncSymbolCardHighlight()
}

function setActiveSymbolHighlight(symbolIndex: number | null): void {
  // Clicking the already-highlighted symbol toggles the highlight off.
  activeSymbolHighlight = activeSymbolHighlight === symbolIndex ? null : symbolIndex
  renderSymbolMatchOverlays()
  syncSymbolCardHighlight()
}

function syncSymbolCardHighlight(): void {
  for (const card of symbolList.querySelectorAll<HTMLElement>('.symbol-card')) {
    const symbolIndex = Number(card.dataset.symbol)
    card.classList.toggle(
      'is-highlighted',
      activeSymbolHighlight !== null && symbolIndex === activeSymbolHighlight,
    )
  }
}

function createSymbolMatchBox(box: SymbolMatchBox, scale: number, pageHeight: number): HTMLButtonElement {
  const bbox = box.bbox
  const node = document.createElement('button')
  node.type = 'button'
  node.className = 'vector-symbol-match-box'
  if (activeSymbolHighlight !== null) {
    node.classList.toggle('is-highlighted', box.symbol === activeSymbolHighlight)
    node.classList.toggle('is-dimmed', box.symbol !== activeSymbolHighlight)
  }
  node.title = `Symbol ${box.symbol + 1} match`
  node.style.left = `${bbox.x0 * scale}px`
  node.style.top = `${(pageHeight - bbox.y1) * scale}px`
  node.style.width = `${Math.max(bbox.width * scale, 6)}px`
  node.style.height = `${Math.max(bbox.height * scale, 6)}px`
  node.addEventListener('click', (event) => {
    event.preventDefault()
    event.stopPropagation()
    setActiveSymbolHighlight(box.symbol)
    scrollToBBoxCenter(bbox)
  })
  return node
}

function renderSymbolMatchOverlays(): void {
  const st = currentPageState
  if (!st) return
  st.overlay.querySelectorAll('.vector-symbol-match-box').forEach((node) => node.remove())
  if (!showSymbolMatches) return
  for (const box of symbolMatchBoxes) {
    if (box.pageNumber !== st.pageNumber) continue
    st.overlay.appendChild(createSymbolMatchBox(box, st.scale, st.pageData.page_size.height_pt))
  }
}

async function runSymbolSearch(): Promise<void> {
  if (!activeDocument) {
    setSymbolStatus('Open a PDF before searching.')
    return
  }
  if (!lastExtractedPages.length) {
    setSymbolStatus('Run extraction first, then search.')
    return
  }
  const group = getSelectedVectorEntityGroup()
  if (!group || activeVectorEntityIndex === null) {
    updateSymbolSearchButton()
    setSymbolStatus('Select a vector entity before searching for symbols.')
    return
  }
  const searchPage = activePageNumber
  const entityIndex = activeVectorEntityIndex
  const cacheKey = getSymbolSearchCacheKey(group, lastExtractedPages)
  const cachedResult = getPageDetectionCache()?.symbolSearches.get(cacheKey)
  if (cachedResult) {
    applySymbolSearchResult(cachedResult)
    setSymbolSearchResultStatus(cachedResult, entityIndex, true)
    return
  }
  isSymbolSearchRunning = true
  updateSymbolSearchButton()
  setSymbolStatus(`Matching symbols inside entity ${entityIndex + 1} on page ${searchPage}...`)
  try {
    const response = await fetch('/api/vector-matcher', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mode: 'symbol_search',
        pdf_url: activeDocument.pdf_url,
        page: searchPage,
        pages: lastExtractedPages,
        entity_root: group.root,
        entity_indices: group.indices,
      }),
    })
    const payload = (await response.json()) as {
      ok: boolean
      error?: string
      result?: SymbolSearchResult
    }
    if (!response.ok || !payload.ok || !payload.result) {
      throw new Error(payload.error || `Symbol search failed: ${response.status}`)
    }
    const result = payload.result
    if (
      activePageNumber !== searchPage ||
      result.entity_root !== group.root
    ) {
      setSymbolStatus('Symbol search result discarded because the page changed.')
      return
    }
    getPageDetectionCache(true)!.symbolSearches.set(cacheKey, result)
    applySymbolSearchResult(result)
    setSymbolSearchResultStatus(result, entityIndex)
  } catch (error) {
    setSymbolStatus(`Symbol search failed: ${error instanceof Error ? error.message : String(error)}`)
  } finally {
    isSymbolSearchRunning = false
    updateSymbolSearchButton()
  }
}

symbolAddPageBtn.addEventListener('click', () => {
  if (!activeDocument) {
    setSymbolStatus('Open a PDF before adding pages.')
    return
  }
  const pages = new Set(parseSymbolPages(symbolPagesInput.value))
  pages.add(activePageNumber)
  symbolPagesInput.value = [...pages].sort((a, b) => a - b).join(', ')
})

symbolExtractBtn.addEventListener('click', () => void runSymbolExtraction())
symbolSearchBtn.addEventListener('click', () => void runSymbolSearch())
extractInfoRunBtn.addEventListener('click', () => void runExtractInfo())
extractInfoCancelBtn.addEventListener('click', () => void cancelExtractInfo())
symbolPagesInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault()
    void runSymbolExtraction()
  }
})

boxDetectBtn.addEventListener('click', async () => {
  if (!activeDocument || !currentPageState) {
    return
  }
  const group = getSelectedVectorEntityGroup()
  if (!group || activeVectorEntityIndex === null) {
    updateVectorEntityActionButtons()
    setStatus('Select a vector entity before detecting boxes.')
    return
  }
  if (
    vectorBoxResult?.page_number === activePageNumber &&
    vectorBoxResult.entity_root === group.root
  ) {
    clearVectorBoxResult()
    setStatus(`Box overlays hidden for entity ${activeVectorEntityIndex + 1}.`)
    return
  }
  const cachedBoxes = getPageDetectionCache()?.boxes.get(group.root)
  if (cachedBoxes) {
    vectorBoxResult = cachedBoxes
    activeVectorBoxIndex = null
    updateDetectButtons()
    renderVectorBoxOverlays()
    setStatus(`Restored ${cachedBoxes.box_count} cached boxes for entity ${activeVectorEntityIndex + 1}.`)
    return
  }
  try {
    boxDetectBtn.disabled = true
    vectorBoxResult = null
    activeVectorBoxIndex = null
    updateDetectButtons()
    renderVectorBoxOverlays()
    setStatus(`Detecting closed vector boxes inside entity ${activeVectorEntityIndex + 1}...`)
    vectorBoxResult = await callVectorBoxDetection(group, activeVectorEntityIndex)
    getPageDetectionCache(true)!.boxes.set(group.root, vectorBoxResult)
    updateDetectButtons()
    renderVectorBoxOverlays()
    setStatus(
      `Detected ${vectorBoxResult.box_count} closed vector box${vectorBoxResult.box_count === 1 ? '' : 'es'} inside entity ${activeVectorEntityIndex + 1}.`,
    )
  } catch (error) {
    vectorBoxResult = null
    activeVectorBoxIndex = null
    updateDetectButtons()
    renderVectorBoxOverlays()
    setStatus(`Box detection failed: ${error instanceof Error ? error.message : String(error)}`)
  } finally {
    boxDetectBtn.disabled = false
    updateVectorEntityActionButtons()
  }
})

circleDetectBtn.addEventListener('click', async () => {
  if (!activeDocument || !currentPageState) {
    return
  }
  const group = getSelectedVectorEntityGroup()
  if (!group || activeVectorEntityIndex === null) {
    updateVectorEntityActionButtons()
    setStatus('Select a vector entity before detecting circles.')
    return
  }
  if (
    vectorCircleResult?.page_number === activePageNumber &&
    vectorCircleResult.entity_root === group.root
  ) {
    clearVectorCircleResult()
    setStatus(`Circle overlays hidden for entity ${activeVectorEntityIndex + 1}.`)
    return
  }
  const cachedCircles = getPageDetectionCache()?.circles.get(group.root)
  if (cachedCircles) {
    vectorCircleResult = cachedCircles
    activeVectorCircleIndex = null
    updateDetectButtons()
    renderVectorCircleOverlays()
    setStatus(`Restored ${cachedCircles.circle_count} cached circles for entity ${activeVectorEntityIndex + 1}.`)
    return
  }
  try {
    circleDetectBtn.disabled = true
    vectorCircleResult = null
    activeVectorCircleIndex = null
    updateDetectButtons()
    renderVectorCircleOverlays()
    setStatus(`Detecting closed vector circles inside entity ${activeVectorEntityIndex + 1}...`)
    vectorCircleResult = await callVectorCircleDetection(group, activeVectorEntityIndex)
    getPageDetectionCache(true)!.circles.set(group.root, vectorCircleResult)
    updateDetectButtons()
    renderVectorCircleOverlays()
    setStatus(
      `Detected ${vectorCircleResult.circle_count} closed vector circle${vectorCircleResult.circle_count === 1 ? '' : 's'} inside entity ${activeVectorEntityIndex + 1}.`,
    )
  } catch (error) {
    vectorCircleResult = null
    activeVectorCircleIndex = null
    updateDetectButtons()
    renderVectorCircleOverlays()
    setStatus(`Circle detection failed: ${error instanceof Error ? error.message : String(error)}`)
  } finally {
    circleDetectBtn.disabled = false
    updateVectorEntityActionButtons()
  }
})

dashedDetectBtn.addEventListener('click', async () => {
  if (!activeDocument || !currentPageState) {
    return
  }
  const group = getSelectedVectorEntityGroup()
  if (!group || activeVectorEntityIndex === null) {
    updateVectorEntityActionButtons()
    setStatus('Select a vector entity before detecting dashed polygons.')
    return
  }
  if (
    vectorDashedResult?.page_number === activePageNumber &&
    vectorDashedResult.entity_root === group.root
  ) {
    clearVectorDashedResult()
    setStatus(`Dashed overlays hidden for entity ${activeVectorEntityIndex + 1}.`)
    return
  }
  const cachedDashed = getPageDetectionCache()?.dashed.get(group.root)
  if (cachedDashed) {
    vectorDashedResult = cachedDashed
    activeVectorDashedIndex = null
    updateDetectButtons()
    renderVectorDashedOverlays()
    setStatus(`Restored ${cachedDashed.dashed_count} cached dashed polygons for entity ${activeVectorEntityIndex + 1}.`)
    return
  }
  try {
    dashedDetectBtn.disabled = true
    vectorDashedResult = null
    activeVectorDashedIndex = null
    updateDetectButtons()
    renderVectorDashedOverlays()
    setStatus(`Detecting closed dashed vector polygons inside entity ${activeVectorEntityIndex + 1}...`)
    vectorDashedResult = await callVectorDashedDetection(group, activeVectorEntityIndex)
    getPageDetectionCache(true)!.dashed.set(group.root, vectorDashedResult)
    updateDetectButtons()
    renderVectorDashedOverlays()
    setStatus(
      `Detected ${vectorDashedResult.dashed_count} closed dashed vector polygon${vectorDashedResult.dashed_count === 1 ? '' : 's'} inside entity ${activeVectorEntityIndex + 1}.`,
    )
  } catch (error) {
    vectorDashedResult = null
    activeVectorDashedIndex = null
    updateDetectButtons()
    renderVectorDashedOverlays()
    setStatus(`Dashed detection failed: ${error instanceof Error ? error.message : String(error)}`)
  } finally {
    dashedDetectBtn.disabled = false
    updateVectorEntityActionButtons()
  }
})

cellDetectBtn.addEventListener('click', async () => {
  if (!activeDocument || !currentPageState) {
    return
  }
  const group = getSelectedVectorEntityGroup()
  if (!group || activeVectorEntityIndex === null) {
    updateVectorEntityActionButtons()
    setStatus('Select a vector entity before detecting cells.')
    return
  }
  if (
    vectorCellResult?.page_number === activePageNumber &&
    vectorCellResult.entity_root === group.root
  ) {
    clearVectorCellResult()
    setStatus(`Cell overlays hidden for entity ${activeVectorEntityIndex + 1}.`)
    return
  }
  const cachedCells = getPageDetectionCache()?.cells.get(group.root)
  if (cachedCells) {
    vectorCellResult = cachedCells
    activeVectorCellIndex = null
    updateDetectButtons()
    renderVectorCellOverlays()
    setStatus(`Restored ${cachedCells.cell_count} cached cells for entity ${activeVectorEntityIndex + 1}.`)
    return
  }
  try {
    cellDetectBtn.disabled = true
    vectorCellResult = null
    activeVectorCellIndex = null
    updateDetectButtons()
    renderVectorCellOverlays()
    setStatus(`Splitting vector intersections and detecting cells inside entity ${activeVectorEntityIndex + 1}...`)
    vectorCellResult = await callVectorCellDetection(group, activeVectorEntityIndex)
    getPageDetectionCache(true)!.cells.set(group.root, vectorCellResult)
    updateDetectButtons()
    renderVectorCellOverlays()
    setStatus(
      `Detected ${vectorCellResult.cell_count} closed vector cell${vectorCellResult.cell_count === 1 ? '' : 's'} inside entity ${activeVectorEntityIndex + 1}.`,
    )
  } catch (error) {
    vectorCellResult = null
    activeVectorCellIndex = null
    updateDetectButtons()
    renderVectorCellOverlays()
    setStatus(`Cell detection failed: ${error instanceof Error ? error.message : String(error)}`)
  } finally {
    cellDetectBtn.disabled = false
    updateVectorEntityActionButtons()
  }
})

zoomInput.addEventListener('change', () => {
  const rawValue = Number.parseFloat(zoomInput.value)
  if (!Number.isFinite(rawValue)) {
    updateZoomLabel()
    return
  }
  const nextZoomFactor = clamp(rawValue / 100, MIN_ZOOM_FACTOR, MAX_ZOOM_FACTOR)
  changeZoom(nextZoomFactor, getViewportCenterAnchor())
})

zoomInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    zoomInput.blur()
  }
})

viewerScroll.addEventListener(
  'wheel',
  (event) => {
    if (!activeDocument) {
      return
    }
    event.preventDefault()
    const anchor = getViewportAnchorFromClientPoint(event.clientX, event.clientY) ?? getViewportCenterAnchor()
    const zoomDelta = Math.exp(-event.deltaY * 0.0015)
    changeZoom(zoomFactor * zoomDelta, anchor)
  },
  { passive: false },
)

viewerScroll.addEventListener('pointerdown', (event) => {
  if (event.button !== 0) {
    return
  }
  const target = event.target as HTMLElement | null
  if (target?.closest('.overlay-item, .vector-match-box, .vector-entity-box, .vector-box-detect-box, .vector-circle-detect-box, .vector-dashed-detect-box, .vector-cell-detect-box, .vector-pin-detect-box, .button, .select, .resize-handle')) {
    return
  }
  isPanning = true
  panStartX = event.clientX
  panStartY = event.clientY
  panScrollLeft = viewerScroll.scrollLeft
  panScrollTop = viewerScroll.scrollTop
  panStartTranslateX = stageTranslateX
  panStartTranslateY = stageTranslateY
  viewerScroll.classList.add('is-panning')
})

window.addEventListener('pointermove', (event) => {
  if (!isPanning) {
    return
  }
  const dx = event.clientX - panStartX
  const dy = event.clientY - panStartY
  const maxScroll = getScrollMax()
  // Overflow axes scroll; underflow axes translate so a small page can still be
  // dragged around freely.
  if (maxScroll.x > 0) {
    viewerScroll.scrollLeft = panScrollLeft - dx
  } else {
    stageTranslateX = panStartTranslateX + dx
  }
  if (maxScroll.y > 0) {
    viewerScroll.scrollTop = panScrollTop - dy
  } else {
    stageTranslateY = panStartTranslateY + dy
  }
  applyStageTransform()
})

window.addEventListener('pointerup', () => {
  isPanning = false
  viewerScroll.classList.remove('is-panning')
})

window.addEventListener('pointercancel', () => {
  isPanning = false
  viewerScroll.classList.remove('is-panning')
})

window.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && isAreaSelectMode) {
    setAreaSelectMode(false)
    setStatus(`Page ${activePageNumber} ready.`)
  }
})

window.addEventListener('resize', async () => {
  if (activeDocument) {
    scheduleRender(activePageNumber, { preserveSelection: true, anchor: getViewportCenterAnchor() })
  }
})

function initLayerToggles(): void {
  document.querySelectorAll<HTMLInputElement>('.layer-toggle').forEach((input) => {
    const kind = input.dataset.layerKind as OverlayKind | undefined
    if (!kind || !(kind in layerVisibility)) {
      return
    }
    input.checked = layerVisibility[kind]
    input.addEventListener('change', () => {
      layerVisibility = { ...layerVisibility, [kind]: input.checked }
      saveLayerVisibility(layerVisibility)
      if (activeDocument) {
        if (activeSelectionId && currentPageState?.pageData) {
          const sel = currentPageState.pageData.items.find((item) => item.id === activeSelectionId)
          if (sel && !isLayerVisible(sel.kind)) {
            activeSelectionId = null
          }
        }
        scheduleRender(activePageNumber, { preserveSelection: true })
      }
    })
  })
}

sourceStashBtn.addEventListener('click', () => {
  if (!pendingPlaygroundSource || !activeVectorMatcherSelection) {
    return
  }
  playgroundStash = {
    pageNumber: activeVectorMatcherSelection.pageNumber,
    queryBBox: { ...activeVectorMatcherSelection.queryBBox },
    shapes: structuredClone(activeVectorMatcherSelection.shapes),
    sourceCode: activeVectorMatcherSelection.sourceCode,
  }
  playgroundShapeScale = 1
  playgroundShapeScaleInput.value = '100'
  updatePlaygroundStashCount(playgroundStash.shapes.length)
  loadPlaygroundCode(pendingPlaygroundSource)
  setPlaygroundStatus('Stashed shape into the playground. The preview is ready for search or compare.')
  updateVectorMatchButton()
  updateSourceCompareButton()
})

vectorOwnerSearchBtn.addEventListener('click', searchSelectedVectorOwnership)
extractJsonSearchButton.addEventListener('click', searchExtractInfoJson)
extractJsonSearchId.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') searchExtractInfoJson()
})

playgroundShapeScaleInput.addEventListener('change', () => {
  const percent = Number.parseFloat(playgroundShapeScaleInput.value)
  const normalizedPercent = Number.isFinite(percent) ? clamp(percent, 10, 400) : 100
  playgroundShapeScale = normalizedPercent / 100
  playgroundShapeScaleInput.value = String(normalizedPercent)
  playgroundHover = null
  playgroundCoords.textContent = 'x: -, y: -'
  renderPlayground()
  setPlaygroundStatus(`Stashed shape preview scaled to ${normalizedPercent}%. Compare shape remains at 100%.`)
})

sourceCompareBtn.addEventListener('click', () => {
  if (playgroundComparison) {
    playgroundComparison = null
    updateSourceCompareButton()
    renderPlayground()
    setPlaygroundStatus('Compare cleared. Playground preview restored.')
    return
  }

  if (!playgroundData || !activeVectorMatcherSelection?.sourceCode) {
    updateSourceCompareButton()
    return
  }

  const candidate = parsePlaygroundSnippet(activeVectorMatcherSelection.sourceCode)
  if (!candidate) {
    setPlaygroundStatus('Unable to parse the selected vector group for compare.')
    return
  }

  playgroundComparison = comparePlaygroundData(playgroundData, candidate)
  updateSourceCompareButton()
  renderPlayground()
  const matched = playgroundComparison.matchedCandidateIndexes.size
  const total = candidate.polylines.length
  setPlaygroundStatus(`Compare: ${matched}/${total} selected path${total === 1 ? '' : 's'} matched at the aligned origin.`)
})

vectorMatchSearchBtn.addEventListener('click', async () => {
  if (!playgroundStash || !activeDocument || !currentPageState) {
    setPlaygroundStatus('Stash a vector shape before searching.')
    return
  }
  try {
    vectorMatchSearchBtn.disabled = true
    const searchScope = 'current' as const
    const totalPages = 1
    const queryBBox = playgroundStash.queryBBox
    const targetPage = playgroundStash.pageNumber
    const searchPage = currentPageState.pageNumber
    const missingVectorRatio = getMissingVectorRatio()
    const missingVectorPercent = missingVectorRatio * 100
    const scaleRange = getScaleRange()
    vectorMatchResults = []
    vectorMatchResultsLabel = 'Results: None'
    renderVectorMatchOverlays()
    setVectorMatchMenuSearching(totalPages)
    setStatus(
      `Searching current page ${searchPage} using the frozen stash from page ${targetPage}, with scale ${scaleRange[0]}-${scaleRange[1]} and ${missingVectorPercent}% missing tolerance...`,
    )
    const result = await callVectorMatcher('match', queryBBox, {
      coordSpace: 'pdf',
      searchScope,
      missingVectorRatio,
      scaleRange,
      targetPage,
      searchPage,
      targetShapes: playgroundStash.shapes,
    })
    if (result.target_page !== targetPage) {
      throw new Error(`Target page mismatch: requested ${targetPage}, received ${result.target_page ?? 'unknown'}.`)
    }
    if (!result.search_pages?.includes(searchPage)) {
      throw new Error(`Search page mismatch: requested ${searchPage}, received ${result.search_pages?.join(', ') || 'unknown'}.`)
    }
    vectorMatchResults = result.matches ?? []
    vectorMatchResultsLabel = 'Results: None'
    renderVectorMatchResultsSelect()
    renderVectorMatchOverlays()
    const searchedPages = result.searched_page_count ?? totalPages
    const pageCount = getVectorMatchPageGroups().length
    setStatus(
      `Found ${vectorMatchResults.length} matching vector group${vectorMatchResults.length === 1 ? '' : 's'} on ${pageCount} page${pageCount === 1 ? '' : 's'} after searching ${searchedPages} page${searchedPages === 1 ? '' : 's'}.`,
    )
    updateVectorMatchButton()
    setPlaygroundStatus(
      `Search complete: ${vectorMatchResults.length} match${vectorMatchResults.length === 1 ? '' : 'es'} on current page ${searchPage}.`,
    )
  } catch (error) {
    vectorMatchResults = []
    vectorMatchResultsLabel = `Search failed: ${error instanceof Error ? error.message : String(error)}`
    renderVectorMatchResultsSelect()
    setStatus(`Vector match failed: ${error instanceof Error ? error.message : String(error)}`)
    updateVectorMatchButton()
  }
})

vectorMatchResultsSelect.addEventListener('change', async () => {
  const pageNumber = Number(vectorMatchResultsSelect.value)
  vectorMatchResultsSelect.value = ''
  if (!Number.isFinite(pageNumber) || !pageNumber) {
    return
  }
  const firstMatch = vectorMatchResults.find((match) => match.page_number === pageNumber)
  await selectPage(pageNumber)
  updatePagerButtons()
  if (firstMatch) {
    scrollToBBoxCenter(firstMatch.bbox_pdf)
  }
})

async function bootstrap(): Promise<void> {
  try {
    initResizableLayout()
    initLayerToggles()
    initPlayground()
    manifest = await loadManifest()
    if (!manifest.documents.length) {
      setStatus('No PDF data available. Run the data generation script first.')
      return
    }
    populateDocumentSelect(manifest.documents)
    await selectDocument(manifest.documents[0].id)
    updatePagerButtons()
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    setStatus(`Load failed: ${message}`)
  }
}

void bootstrap()
