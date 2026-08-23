(function exposeReaderUtils(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.XiaxiaReaderUtils = api;
})(typeof window !== "undefined" ? window : globalThis, function buildReaderUtils() {
  "use strict";

  function clamp(value, min, max) {
    const numeric = Number(value);
    return Math.max(min, Math.min(max, Number.isFinite(numeric) ? numeric : min));
  }

  function calculatePageCount(scrollWidth, clientWidth, gap = 0) {
    const pageSpan = Math.max(1, Number(clientWidth) + Number(gap || 0));
    return Math.max(1, Math.ceil((Number(scrollWidth) + Number(gap || 0)) / pageSpan));
  }

  function selectionMenuPosition({ rect, viewport, mobile, menuWidth = 180, menuHeight = 50, toolbarBottom = 76, margin = 12 }) {
    const viewportLeft = Number(viewport.offsetLeft || 0);
    const viewportTop = Number(viewport.offsetTop || 0);
    const viewportWidth = Number(viewport.width);
    const viewportHeight = Number(viewport.height);
    const center = Number(rect.left) + Number(rect.width) / 2;
    const half = menuWidth / 2;
    const left = clamp(center, viewportLeft + half + margin, viewportLeft + viewportWidth - half - margin);
    if (mobile) {
      const viewportBottom = viewportTop + viewportHeight - Number(viewport.bottomInset || margin);
      const clearance = 28;
      const belowTop = Number(rect.bottom) + clearance;
      if (belowTop + menuHeight <= viewportBottom) {
        return { left, top: belowTop, bottom: null, placement: "mobile-below" };
      }
      const aboveBottom = Number(rect.top) - clearance;
      if (aboveBottom - menuHeight >= viewportTop + margin) {
        return { left, top: aboveBottom, bottom: null, placement: "mobile-above" };
      }
      return {
        left,
        top: clamp(Number(rect.bottom) + margin, viewportTop + margin, viewportBottom - menuHeight),
        bottom: null,
        placement: "mobile-clamped",
      };
    }
    const roomAbove = Number(rect.top) - Math.max(toolbarBottom, viewportTop);
    if (roomAbove >= 54) {
      return { left, top: Number(rect.top), bottom: null, placement: "above" };
    }
    return {
      left,
      top: Math.min(viewportTop + viewportHeight - 58, Number(rect.bottom) + 10),
      bottom: null,
      placement: "below",
    };
  }

  function swipeDirection(start, end, hasSelection) {
    if (!start || !end || hasSelection) return null;
    const dx = Number(end.x) - Number(start.x);
    const dy = Number(end.y) - Number(start.y);
    if (Math.abs(dx) < 48 || Math.abs(dx) <= Math.abs(dy) * 1.15) return null;
    return dx < 0 ? "next" : "previous";
  }

  function syncSignature(annotations, thoughts) {
    const user = (annotations || []).map((item) => [
      item.id, item.updated_at, item.reply_updated_at, item.xiaxia_response,
      item.start_block_id, item.start_offset, item.end_block_id, item.end_offset,
    ]);
    const xiaxia = (thoughts || []).map((item) => [
      item.id, item.updated_at, item.scope, item.start_block_id,
      item.start_offset, item.end_block_id, item.end_offset,
      item.user_reply_updated_at, item.user_response,
    ]);
    return JSON.stringify([user, xiaxia]);
  }

  return { clamp, calculatePageCount, selectionMenuPosition, swipeDirection, syncSignature };
});
