import * as d3 from "d3";

const RELATION_LABELS = {
  current: "Текущая страница",
  mutual: "Взаимная ссылка",
  incoming: "Ссылается на текущую",
  outgoing: "На нее ссылается текущая",
};

const NODE_COLORS = {
  current: "#f59e0b",
  mutual: "#10b981",
  incoming: "#0ea5e9",
  outgoing: "#8b5cf6",
};

const IGNORED_NODE_COLOR = "#94a3b8";

const EDGE_COLORS = {
  incoming: "#38bdf8",
  outgoing: "#a78bfa",
};

const truncate = (value, maxLength = 42) => {
  if (!value) return "";
  return value.length > maxLength ? `${value.slice(0, maxLength - 1)}…` : value;
};

const isNodeVisibleForFilter = (node, filter) => {
  if (filter === "all") return true;
  if (filter === "current") return node.relation === "current";
  if (node.relation === "current") return true;
  if (filter === "ignored") return Boolean(node.is_ignored);
  if (filter === "external") return Boolean(node.is_external);
  return node.relation === filter;
};

const setSelectedNode = (node) => {
  const titleEl = document.getElementById("document-links-selected-title");
  const relationEl = document.getElementById("document-links-selected-relation");
  const detailEl = document.getElementById("document-links-selected-detail");
  const uriEl = document.getElementById("document-links-selected-uri");
  const uriEmptyEl = document.getElementById("document-links-selected-uri-empty");

  if (!titleEl || !relationEl || !detailEl || !uriEl || !uriEmptyEl) {
    return;
  }

  titleEl.textContent = node.title || "Без названия";
  relationEl.textContent = RELATION_LABELS[node.relation] || node.relation || "";
  detailEl.textContent = node.detail_url || "";
  detailEl.href = node.detail_url || "#";

  if (node.uri) {
    uriEl.textContent = node.uri;
    uriEl.href = node.uri;
    uriEl.classList.remove("hidden");
    uriEmptyEl.classList.add("hidden");
  } else {
    uriEl.textContent = "";
    uriEl.href = "#";
    uriEl.classList.add("hidden");
    uriEmptyEl.classList.remove("hidden");
  }

  if (node.is_ignored) {
    relationEl.textContent += " · игнорируется";
  } else if (node.is_external) {
    relationEl.textContent += " · другой домен";
  }
};

const buildGraph = () => {
  const container = document.getElementById("document-links-graph");
  const dataEl = document.getElementById("document-links-graph-data");
  if (!container || !dataEl) {
    return;
  }

  const graph = JSON.parse(dataEl.textContent || "{}");
  if (!Array.isArray(graph.nodes) || !Array.isArray(graph.links) || graph.nodes.length === 0) {
    return;
  }

  const rect = container.getBoundingClientRect();
  const width = Math.max(320, Math.floor(rect.width || container.clientWidth || 600));
  const height = Math.max(320, Math.floor(rect.height || 420));
  const currentNodeId = graph.currentNodeId;

  container.innerHTML = "";

  const svg = d3
    .select(container)
    .append("svg")
    .attr("viewBox", `0 0 ${width} ${height}`)
    .attr("class", "h-full w-full");

  const defs = svg.append("defs");
  Object.entries(EDGE_COLORS).forEach(([relation, color]) => {
    defs
      .append("marker")
      .attr("id", `document-links-arrow-${relation}`)
      .attr("viewBox", "0 -5 10 10")
      .attr("refX", 20)
      .attr("refY", 0)
      .attr("markerWidth", 6)
      .attr("markerHeight", 6)
      .attr("orient", "auto")
      .append("path")
      .attr("fill", color)
      .attr("d", "M0,-5L10,0L0,5");
  });

  const nodes = graph.nodes.map((node) => ({ ...node }));
  const links = graph.links.map((link) => ({ ...link }));
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  let activeFilter = "all";

  const simulation = d3
    .forceSimulation(nodes)
    .force(
      "link",
      d3
        .forceLink(links)
        .id((d) => d.id)
        .distance((link) => (link.source.id === currentNodeId || link.target.id === currentNodeId ? 100 : 140))
    )
    .force("charge", d3.forceManyBody().strength(-420))
    .force("center", d3.forceCenter(width / 2, height / 2))
    .force("collide", d3.forceCollide().radius((d) => (d.id === currentNodeId ? 34 : 24)))
    .force(
      "radial",
      d3.forceRadial(
        (d) => (d.id === currentNodeId ? 0 : Math.min(width, height) * 0.28),
        width / 2,
        height / 2
      ).strength((d) => (d.id === currentNodeId ? 1 : 0.08))
    );

  const link = svg
    .append("g")
    .attr("stroke-opacity", 0.9)
    .selectAll("line")
    .data(links)
    .join("line")
    .attr("stroke", (d) => EDGE_COLORS[d.relation] || "#94a3b8")
    .attr("stroke-width", (d) => (d.relation === "incoming" ? 1.8 : 2.4))
    .attr("marker-end", (d) => `url(#document-links-arrow-${d.relation})`);

  const node = svg
    .append("g")
    .selectAll("g")
    .data(nodes)
    .join("g")
    .attr("class", "cursor-pointer");

  node
    .append("circle")
    .attr("r", (d) => (d.id === currentNodeId ? 18 : d.relation === "mutual" ? 13 : 11))
    .attr("fill", (d) => (d.is_ignored ? IGNORED_NODE_COLOR : NODE_COLORS[d.relation] || "#64748b"))
    .attr("fill-opacity", (d) => (d.is_external && !d.is_ignored ? 0.45 : 0.95))
    .attr("stroke", "#ffffff")
    .attr("stroke-width", 2)
    .attr("stroke-opacity", (d) => (d.is_external && !d.is_ignored ? 0.55 : 1));

  node
    .append("text")
    .text((d) => truncate(d.title))
    .attr("text-anchor", "middle")
    .attr("dy", (d) => (d.id === currentNodeId ? 34 : 28))
    .attr("fill", "currentColor")
    .attr("font-size", 11)
    .attr("opacity", (d) => (d.is_external ? 0.72 : 1))
    .attr("class", "pointer-events-none fill-base-content");

  node.append("title").text((d) => `${d.title}\n${d.uri || ""}`);

  node.on("click", (_event, d) => {
    setSelectedNode(d);
  });

  node.call(
    d3
      .drag()
      .on("start", (event, d) => {
        if (!event.active) simulation.alphaTarget(0.3).restart();
        d.fx = d.x;
        d.fy = d.y;
      })
      .on("drag", (event, d) => {
        d.fx = event.x;
        d.fy = event.y;
      })
      .on("end", (event, d) => {
        if (!event.active) simulation.alphaTarget(0);
        if (d.id !== currentNodeId) {
          d.fx = null;
          d.fy = null;
        }
      })
  );

  simulation.on("tick", () => {
    link
      .attr("x1", (d) => d.source.x)
      .attr("y1", (d) => d.source.y)
      .attr("x2", (d) => d.target.x)
      .attr("y2", (d) => d.target.y);

    node.attr("transform", (d) => `translate(${d.x},${d.y})`);
  });

  const updateFilterButtonStyles = () => {
    const buttons = document.querySelectorAll("[data-link-filter]");
    buttons.forEach((button) => {
      const isActive = button.getAttribute("data-link-filter") === activeFilter;
      button.setAttribute("data-filter-active", String(isActive));
      button.classList.toggle("bg-base-content", isActive);
      button.classList.toggle("text-base-100", isActive);
      button.classList.toggle("border-base-content", isActive);
      button.classList.toggle("bg-base-100", !isActive);
    });
  };

  const applyFilter = (filter) => {
    activeFilter = filter;
    const visibleNodeIds = new Set(
      nodes.filter((nodeItem) => isNodeVisibleForFilter(nodeItem, filter)).map((nodeItem) => nodeItem.id)
    );

    node
      .style("display", (nodeItem) => (visibleNodeIds.has(nodeItem.id) ? null : "none"))
      .style("pointer-events", (nodeItem) => (visibleNodeIds.has(nodeItem.id) ? "auto" : "none"));

    link.style("display", (linkItem) => {
      const sourceId = typeof linkItem.source === "string" ? linkItem.source : linkItem.source.id;
      const targetId = typeof linkItem.target === "string" ? linkItem.target : linkItem.target.id;
      return visibleNodeIds.has(sourceId) && visibleNodeIds.has(targetId) ? null : "none";
    });

    updateFilterButtonStyles();
    const currentOrFirstVisible =
      nodesById.get(currentNodeId) ||
      nodes.find((nodeItem) => visibleNodeIds.has(nodeItem.id)) ||
      nodes[0];
    if (currentOrFirstVisible) {
      setSelectedNode(currentOrFirstVisible);
    }
  };

  const legendButtons = document.querySelectorAll("[data-link-filter]");
  legendButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const nextFilter = button.getAttribute("data-link-filter") || "all";
      applyFilter(nextFilter);
    });
  });

  const currentNode = nodes.find((nodeItem) => nodeItem.id === currentNodeId) || nodes[0];
  currentNode.fx = width / 2;
  currentNode.fy = height / 2;
  setSelectedNode(currentNode);
  applyFilter("all");
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", buildGraph);
} else {
  buildGraph();
}
