// Small behaviours without inline JavaScript (the Content-Security-Policy forbids it).
// <form data-confirm="Are you sure?"> asks for confirmation before submitting.
document.addEventListener("submit", function (event) {
  var form = event.target;
  var message = form && form.getAttribute && form.getAttribute("data-confirm");
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
}, true);
