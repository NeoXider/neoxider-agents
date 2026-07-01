/* Draggable left/right panel splitters, width persisted in localStorage. */
function makeResizer(handleId, panelId, side) {
  const handle = $("#" + handleId), panel = $("#" + panelId);
  const saved = localStorage.getItem("agentgui_w_" + panelId);
  if (saved) panel.style.width = saved + "px";
  handle.addEventListener("mousedown", e => {
    e.preventDefault();
    const startX = e.clientX, startW = panel.getBoundingClientRect().width;
    handle.classList.add("drag");
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    const onMove = ev => {
      let dx = ev.clientX - startX;
      if (side === "right") dx = -dx;
      const w = Math.max(160, Math.min(640, Math.round(startW + dx)));
      panel.style.width = w + "px";
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      handle.classList.remove("drag");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      localStorage.setItem("agentgui_w_" + panelId, parseInt(panel.style.width, 10));
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}
