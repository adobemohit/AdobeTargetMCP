(function () {
  const nav = document.querySelector(".site-nav");
  const toggle = document.querySelector(".menu-toggle");
  const navLinks = document.querySelector(".nav-links");
  const sections = document.querySelectorAll("section[id]");
  const navAnchors = document.querySelectorAll(".nav-links a");

  if (toggle && navLinks) {
    toggle.addEventListener("click", function () {
      navLinks.classList.toggle("open");
    });

    navLinks.querySelectorAll("a").forEach(function (link) {
      link.addEventListener("click", function () {
        navLinks.classList.remove("open");
      });
    });
  }

  window.addEventListener("scroll", function () {
    if (!nav) return;
    nav.style.background =
      window.scrollY > 40
        ? "rgba(11, 15, 23, 0.95)"
        : "rgba(11, 15, 23, 0.82)";
  });

  const observer = new IntersectionObserver(
    function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        const id = entry.target.getAttribute("id");
        navAnchors.forEach(function (anchor) {
          anchor.classList.toggle(
            "active",
            anchor.getAttribute("href") === "#" + id
          );
        });
      });
    },
    { rootMargin: "-40% 0px -50% 0px" }
  );

  sections.forEach(function (section) {
    observer.observe(section);
  });

  document.querySelectorAll(".faq-question").forEach(function (button) {
    button.addEventListener("click", function () {
      const item = button.closest(".faq-item");
      const isOpen = item.classList.contains("open");

      document.querySelectorAll(".faq-item").forEach(function (faq) {
        faq.classList.remove("open");
      });

      if (!isOpen) {
        item.classList.add("open");
      }
    });
  });
})();
