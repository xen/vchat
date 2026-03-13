document.addEventListener("DOMContentLoaded", () => {
  // Check if the element with ID 'cookie-policy' exists before accessing it
  const cookiePolicyElement = document.querySelector('#cookie-policy');
  const acceptButton = document.querySelector('#cookie-policy-accept');

  if (cookiePolicyElement && !localStorage.getItem('cookie_ok')) {
    cookiePolicyElement.style.display = "flex";
  }

  if (acceptButton) {
    acceptButton.addEventListener('click', () => {
      localStorage.setItem('cookie_ok', "1");
      if (cookiePolicyElement) {
        cookiePolicyElement.style.display = "none";
      }
    });
  }
});
