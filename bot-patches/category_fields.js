// Added by kleinanzeiger: read the category-specific fields of the ad form.
// @ts-check

(
  /** Describe every attributeMap field: label, required, preset value and options. */
  async () => {
    const sleep = (/** @type {number} */ ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const clean = (/** @type {string | null | undefined} */ s) => (s || "").replace(/\s+/g, " ").trim();
    /** @param {Element} button */
    const reactOptions = (button) => {
      const fiberKey = Object.keys(button).find((k) => k.startsWith("__reactFiber"));
      // React internals are untyped.
      let fiber = fiberKey ? /** @type {any} */ (button)[fiberKey] : null;
      for (let depth = 0; depth < 20 && fiber; depth++, fiber = fiber.return) {
        if (fiber.memoizedProps && Array.isArray(fiber.memoizedProps.options)) return fiber.memoizedProps.options;
      }
      return null;
    };
    const fields = [];
    for (const hidden of document.querySelectorAll('input[type="hidden"][name^="attributeMap["]')) {
      const key = /** @type {HTMLInputElement} */ (hidden).name.slice("attributeMap[".length, -1);
      const name = key.split(".").pop() || key;
      const button = document.getElementById(key);
      const labelElement = document.querySelector(`label[for="${CSS.escape(key)}"]`);
      const radios = [...document.querySelectorAll(`input[type="radio"][name="attributeMap[${CSS.escape(key)}]"]`)];
      const rawLabel = clean(labelElement?.textContent) || clean(radios[0]?.closest("fieldset")?.querySelector("legend")?.textContent) || name;
      const optional = /\(optional\)/i.test(rawLabel);
      /** @type {{value: string, label: string}[]} */
      let options = [];
      if (radios.length) {
        options = radios.map((radio) => ({
          value: /** @type {HTMLInputElement} */ (radio).value,
          label: clean(document.querySelector(`label[for="${CSS.escape(radio.id)}"]`)?.textContent),
        }));
      } else if (button && button.getAttribute("role") === "combobox") {
        button.dispatchEvent(new MouseEvent("click", {bubbles: true}));
        await sleep(800);
        const listbox = document.getElementById(button.getAttribute("aria-controls") || "")
          || document.querySelector('[role="listbox"]');
        const labels = listbox ? [...listbox.querySelectorAll('[role="option"]')].map((o) => clean(o.textContent)) : [];
        const values = reactOptions(button);
        options = labels.map((label, index) => ({
          value: values && values.length === labels.length && values[index] ? String(values[index].value ?? "") : "",
          label,
        })).filter((option) => option.label && option.label !== "Bitte wählen");
        button.dispatchEvent(new MouseEvent("click", {bubbles: true}));
        await sleep(200);
        document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
      }
      fields.push({
        key,
        attribute: `${name}_s`,
        label: rawLabel.replace(/\s*\(optional\)\s*/i, " ").trim(),
        required: !optional,
        preset: /** @type {HTMLInputElement} */ (hidden).value || "",
        options: options.slice(0, 300),
      });
    }
    return fields;
  }
);
