// CDN TinyMCE loader for quick integration
(function() {
  var script = document.createElement('script');
  script.src = 'https://cdn.tiny.cloud/1/x893gc30ptbx1gum2466jfkkmxhxy668b7flxqge8njyzt6o/tinymce/6/tinymce.min.js';
  script.referrerPolicy = 'origin';
  script.onload = function() {
    tinymce.init({
      selector: 'textarea[name="description"]',
      menubar: false,
      plugins: 'lists link image code',
      toolbar: 'undo redo | bold italic underline | bullist numlist | link | code',
      height: 200,
      branding: false
    });
  };
  document.head.appendChild(script);
})();
