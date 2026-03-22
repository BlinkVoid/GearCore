# Skill: Web Research

## Overview
Browser automation tools for web research, scraping, and navigation using Playwright.

## Workflow
1. Navigate to a URL
2. Take a screenshot to verify the page loaded
3. Interact with page elements as needed
4. Extract information from the rendered page
5. Close the browser when done

## Tools

### browser_navigate
Open a URL in the browser.
```bash
gearcore call playwright browser_navigate '{"url": "https://example.com"}'
```

### browser_screenshot
Capture the current page state.
```bash
gearcore call playwright browser_screenshot '{}'
```

### browser_click
Click a page element using CSS selector.
```bash
gearcore call playwright browser_click '{"selector": "#submit-btn"}'
```

### browser_type
Type text into a page element.
```bash
gearcore call playwright browser_type '{"selector": "#search-input", "text": "query"}'
```

### browser_tab_list
List open browser tabs.
```bash
gearcore call playwright browser_tab_list '{}'
```

### browser_close
Close the browser to free resources.
```bash
gearcore call playwright browser_close '{}'
```

## Best Practices
- Take a screenshot after navigation to verify the page loaded correctly
- Use CSS selectors for reliable element targeting
- Close browser tabs when done to free resources
- Be respectful of rate limits and robots.txt
