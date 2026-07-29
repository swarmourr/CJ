document.addEventListener("DOMContentLoaded", function () {
  if (typeof mermaid === "undefined") return;
  mermaid.initialize({
    startOnLoad: true,
    theme: "dark",
    themeVariables: {
      primaryColor:        "#1a4731",
      primaryTextColor:    "#7dc829",
      primaryBorderColor:  "#7dc829",
      lineColor:           "#7dc829",
      secondaryColor:      "#2e3440",
      tertiaryColor:       "#0f2318",
      mainBkg:             "#1a4731",
      nodeBorder:          "#7dc829",
      clusterBkg:          "#0f2318",
      titleColor:          "#7dc829",
      edgeLabelBackground: "#0f2318",
      fontFamily:          "Segoe UI, sans-serif",
    },
  });
});
