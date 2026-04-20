import {
  createTable,
  getCoreRowModel,
  getPaginationRowModel,
  getFilteredRowModel,
  getSortedRowModel,
} from '@tanstack/table-core'

const flexRender = (comp, props) => {
  if (typeof comp === 'function') {
    return comp(props)
  }
  return comp
}

const cloneStateSlice = (value) => {
  if (Array.isArray(value)) {
    return value.map((item) => cloneStateSlice(item))
  }
  if (value && typeof value === 'object') {
    return Object.entries(value).reduce((acc, [key, val]) => {
      acc[key] = cloneStateSlice(val)
      return acc
    }, {})
  }
  return value
}

const escapeHtml = (value) => {
  if (value === null || value === undefined) {
    return ''
  }
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

const formatBytes = (value) => {
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return '0 B'
  }
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  const quotient = bytes / 1024 ** exponent
  const formatted = quotient >= 100
    ? Math.round(quotient).toString()
    : quotient >= 10
      ? (Math.round(quotient * 10) / 10).toString()
      : (Math.round(quotient * 100) / 100).toString()
  return `${formatted} ${units[exponent]}`
}

const compareIsoDates = (rowA, rowB, columnId) => {
  const a = new Date(rowA.getValue(columnId) || 0).getTime() || 0
  const b = new Date(rowB.getValue(columnId) || 0).getTime() || 0
  return a - b
}

const compareNumbers = (rowA, rowB, columnId) => {
  const a = Number(rowA.getValue(columnId)) || 0
  const b = Number(rowB.getValue(columnId)) || 0
  return a - b
}

const compareStrings = (rowA, rowB, columnId) => {
  const a = (rowA.getValue(columnId) ?? '').toString().toLowerCase()
  const b = (rowB.getValue(columnId) ?? '').toString().toLowerCase()
  return a.localeCompare(b)
}

const resolveDocumentType = (meta, fallback) => {
  const rawMetaType = meta && typeof meta.doc_type === 'string' ? meta.doc_type.trim() : ''
  if (rawMetaType) return rawMetaType
  const rawFallback = typeof fallback === 'string' ? fallback.trim() : ''
  return rawFallback || 'file'
}

const columns = [
  {
    id: 'document_type',
    accessorFn: (row) => resolveDocumentType(row?.meta, row?.document_type),
    header: 'Тип',
    cell: (info) => {
      const typeKey = escapeHtml(info.getValue() || 'file')
      return `
        <div class="flex items-center gap-2">
          <iconify-icon icon="lucide:file-text" class="size-4 opacity-70"></iconify-icon>
          <span class="text-xs opacity-70">${typeKey}</span>
        </div>
      `
    },
    enableSorting: true,
    sortingFn: compareStrings,
  },
  {
    accessorKey: 'title',
    header: 'Название',
    cell: (info) => `<div class="font-medium">${escapeHtml(info.getValue() || 'Без названия')}</div>`,
    enableSorting: true,
    sortingFn: compareStrings,
  },
  {
    id: 'size_chunks',
    header: 'Размер / чанки',
    accessorFn: (row) => Number(row?.size_bytes ?? 0),
    cell: (info) => {
      const bytes = Number(info.row.original.size_bytes || 0)
      const chunkCount = Number(info.row.original.chunk_count || 0)
      return `
        <div class="flex flex-col gap-0.5 text-sm">
          <span class="font-medium">${escapeHtml(formatBytes(bytes))}</span>
          <span class="opacity-70">${escapeHtml(String(chunkCount))} чанков</span>
        </div>
      `
    },
    enableSorting: true,
    sortingFn: compareNumbers,
  },
  {
    accessorKey: 'created_at',
    header: 'Добавлен',
    cell: (info) => {
      const value = info.getValue()
      if (!value) return '-'
      return `<div class="text-sm opacity-70">${new Date(value).toLocaleDateString()}</div>`
    },
    enableSorting: true,
    sortingFn: compareIsoDates,
  },
  {
    id: 'view',
    header: '',
    cell: (info) => `
      <div class="flex justify-center">
        <button
          type="button"
          class="btn btn-ghost btn-xs"
          data-file-action="view"
          data-file-id="${info.row.original.id}"
          title="Открыть содержимое файла"
        >
          <iconify-icon icon="lucide:eye" class="size-4"></iconify-icon>
        </button>
      </div>
    `,
    enableSorting: false,
  },
  {
    id: 'actions',
    header: 'Действия',
    cell: (info) => {
      const id = info.row.original.id
      return `
        <div class="flex items-center gap-2">
          <a href="/files/download/${id}"
             class="btn btn-square btn-ghost btn-sm"
             title="Скачать"
             target="_blank"
             rel="noopener noreferrer">
            <iconify-icon icon="lucide:download" class="size-4"></iconify-icon>
          </a>
          <button type="button"
                  class="btn btn-square btn-ghost btn-sm text-error"
                  title="Удалить"
                  data-file-action="delete"
                  data-file-id="${id}">
            <iconify-icon icon="lucide:trash" class="size-4"></iconify-icon>
          </button>
        </div>
      `
    },
    enableSorting: false,
  },
]

window.useFilesDataTable = () => ({
  flexRender,
  loading: true,
  headerGroups: [],
  visibleRows: [],
  filteredRowCount: 0,
  pageIndex: 0,
  pageSize: 10,
  pageCount: 0,
  canPreviousPage: false,
  canNextPage: false,
  rangeStart: 0,
  rangeEnd: 0,
  search: '',
  table: null,
  state: null,
  data: [],
  actionListener: null,
  csrfToken: null,
  documentModal: null,
  documentModalContent: null,
  init() {
    this.state = {
      pagination: { pageSize: 10, pageIndex: 0 },
      globalFilter: '',
      sorting: [],
    }
    this.initFromUrl()
    const component = this
    this.table = createTable({
      state: this.state,
      data: this.data,
      columns,
      getCoreRowModel: getCoreRowModel(),
      getPaginationRowModel: getPaginationRowModel(),
      getFilteredRowModel: getFilteredRowModel(),
      getSortedRowModel: getSortedRowModel(),
      autoResetPageIndex: false,
      onStateChange: (updater) => {
        const newState = typeof updater === 'function' ? updater(component.state) : updater
        Object.assign(component.state, newState)
        component.updateDerivedState()
        component.updateUrl()
      },
    })
    Object.entries(this.table.initialState).forEach(([key, value]) => {
      if (this.state[key] === undefined) {
        this.state[key] = cloneStateSlice(value)
      }
    })
    this.csrfToken = this.extractCsrfToken()
    this.$nextTick?.(() => this.setupActionDelegation())
    this.updateDerivedState()
    this.fetchFiles()
  },
  initFromUrl() {
    if (typeof window === 'undefined') return
    const params = new URLSearchParams(window.location.search)
    const search = params.get('search')
    const page = params.get('page')
    if (search) {
      this.state.globalFilter = search
      this.search = search
    }
    if (page) {
      const pageIndex = parseInt(page, 10) - 1
      if (!isNaN(pageIndex) && pageIndex >= 0) {
        this.state.pagination.pageIndex = pageIndex
      }
    }
  },
  updateUrl() {
    if (typeof window === 'undefined') return
    const url = new URL(window.location)
    const params = url.searchParams
    if (this.state.globalFilter) {
      params.set('search', this.state.globalFilter)
    } else {
      params.delete('search')
    }
    if (this.state.pagination.pageIndex > 0) {
      params.set('page', this.state.pagination.pageIndex + 1)
    } else {
      params.delete('page')
    }
    const newUrl = url.toString()
    if (newUrl !== window.location.href) {
      window.history.pushState({}, '', newUrl)
    }
  },
  fetchFiles() {
    this.loading = true
    fetch('/files/json')
      .then((res) => res.json())
      .then((jsonData) => {
        this.data = Array.isArray(jsonData) ? jsonData : []
        this.table.setOptions((prev) => ({ ...prev, data: this.data }))
        this.loading = false
        this.updateDerivedState()
      })
      .catch((err) => {
        console.error('[FilesDataTable] Failed to load files:', err)
        this.loading = false
        this.updateDerivedState()
      })
  },
  updateDerivedState() {
    if (!this.table) return
    this.headerGroups = [...this.table.getHeaderGroups()]
    this.visibleRows = [...this.table.getRowModel().rows]
    this.filteredRowCount = this.table.getFilteredRowModel().rows.length
    this.pageCount = this.table.getPageCount()
    this.canPreviousPage = this.table.getCanPreviousPage()
    this.canNextPage = this.table.getCanNextPage()
    this.pageIndex = this.state.pagination.pageIndex
    this.pageSize = this.state.pagination.pageSize
    if (this.filteredRowCount === 0) {
      this.rangeStart = 0
      this.rangeEnd = 0
    } else {
      this.rangeStart = this.pageIndex * this.pageSize + 1
      this.rangeEnd = Math.min((this.pageIndex + 1) * this.pageSize, this.filteredRowCount)
    }
  },
  nextPage() {
    this.table?.nextPage()
  },
  prevPage() {
    this.table?.previousPage()
  },
  updateSearch() {
    this.table?.setPageIndex(0)
    this.table?.setGlobalFilter(this.search)
  },
  extractCsrfToken() {
    if (typeof document === 'undefined') return null
    const attr = document.body?.getAttribute('hx-headers')
    if (!attr) return null
    try {
      const parsed = JSON.parse(attr)
      return parsed?.['X-CSRFToken'] || null
    } catch {
      return null
    }
  },
  setupActionDelegation() {
    if (this.actionListener) return
    const body = this.$refs?.filesBody
    if (!body) return
    this.actionListener = (event) => {
      const target = event.target.closest('[data-file-action]')
      if (!target) return
      const fileId = target.dataset.fileId
      if (!fileId) return
      if (target.dataset.fileAction === 'view') {
        this.viewDocument(fileId)
      } else if (target.dataset.fileAction === 'delete') {
        this.deleteFile(fileId)
      }
    }
    body.addEventListener('click', this.actionListener)
  },
  ensureDocumentModal() {
    if (typeof document === 'undefined') {
      return false
    }
    if (!this.documentModal || !this.documentModalContent) {
      this.documentModal = document.getElementById('document_modal')
      this.documentModalContent = document.getElementById('document-content')
    }
    return Boolean(this.documentModal && this.documentModalContent)
  },
  async viewDocument(fileId) {
    window.open(`/document/${fileId}`, '_blank', 'noopener')
  },
  async deleteFile(fileId) {
    if (!window.confirm('Вы уверены, что хотите удалить этот файл?')) {
      return
    }
    try {
      const res = await fetch(`/actions/delete_file/${fileId}`, {
        method: 'POST',
        headers: this.csrfToken ? { 'X-CSRFToken': this.csrfToken } : {},
      })
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`)
      }
      this.fetchFiles()
    } catch (error) {
      console.error('[FilesDataTable] Failed to delete file:', error)
      window.alert('Не удалось удалить файл')
    }
  },
})
