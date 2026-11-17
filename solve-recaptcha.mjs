import { chromium } from "playwright-core";
import { solve } from "recaptcha-solver";

const port = process.argv[2];
const targetUrl = process.argv[3];
const mainFrameSelector = "iframe[title='reCAPTCHA']";
const challengeFrameSelector =
    "iframe[src*='/recaptcha/api2/bframe'], iframe[src*='/recaptcha/enterprise/bframe']";
const visibleChallengeFrameSelector =
    "iframe[src*='/recaptcha/api2/bframe']:visible, iframe[src*='/recaptcha/enterprise/bframe']:visible";

function solverCompatiblePage(page) {
    const responseListeners = new WeakMap();
    let compatiblePage;

    const translateSelector = (selector) =>
        typeof selector === "string" &&
        selector.includes("www.google.com/recaptcha/") &&
        selector.includes("/bframe")
            ? visibleChallengeFrameSelector
            : selector;

    compatiblePage = new Proxy(page, {
        get(target, property) {
            if (property === "waitForSelector") {
                return (selector, options) =>
                    target.waitForSelector(translateSelector(selector), options);
            }
            if (property === "$") {
                return (selector) => target.$(translateSelector(selector));
            }
            if (property === "on") {
                return (event, listener) => {
                    if (event !== "response") {
                        target.on(event, listener);
                        return compatiblePage;
                    }
                    const wrapped = (response) => {
                        const compatibleResponse = new Proxy(response, {
                            get(responseTarget, responseProperty) {
                                if (responseProperty === "url") {
                                    return () =>
                                        responseTarget
                                            .url()
                                            .replace(
                                                "https://www.recaptcha.net/",
                                                "https://www.google.com/",
                                            );
                                }
                                const value = Reflect.get(
                                    responseTarget,
                                    responseProperty,
                                    responseTarget,
                                );
                                return typeof value === "function"
                                    ? value.bind(responseTarget)
                                    : value;
                            },
                        });
                        return listener(compatibleResponse);
                    };
                    responseListeners.set(listener, wrapped);
                    target.on(event, wrapped);
                    return compatiblePage;
                };
            }
            if (property === "off") {
                return (event, listener) => {
                    target.off(event, responseListeners.get(listener) ?? listener);
                    responseListeners.delete(listener);
                    return compatiblePage;
                };
            }
            const value = Reflect.get(target, property, target);
            return typeof value === "function" ? value.bind(target) : value;
        },
    });

    return compatiblePage;
}

if (!port || !targetUrl) {
    console.error("Usage: node solve-recaptcha.mjs <cdp-port> <page-url>");
    process.exit(2);
}

try {
    const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
    const pages = browser.contexts().flatMap((context) => context.pages());
    const page =
        pages.find((candidate) => candidate.url() === targetUrl) ??
        pages.find((candidate) =>
            candidate.url().includes("/support/discussions/topics/new"),
        );

    if (!page) {
        throw new Error(`Could not find the topic page: ${targetUrl}`);
    }

    const mainFrameElement = await page.waitForSelector(mainFrameSelector, {
        state: "attached",
        timeout: 30000,
    });
    const mainFrame = await mainFrameElement.contentFrame();
    if (!mainFrame) {
        throw new Error("Could not access the reCAPTCHA checkbox frame");
    }

    const checkbox = await mainFrame.waitForSelector("#recaptcha-anchor", {
        state: "visible",
        timeout: 10000,
    });
    if ((await checkbox.getAttribute("aria-checked")) !== "true") {
        await checkbox.click();
    }

    try {
        await mainFrame.waitForFunction(
            () =>
                document
                    .querySelector("#recaptcha-anchor")
                    ?.getAttribute("aria-checked") === "true",
            undefined,
            { timeout: 3000 },
        );
        console.log("reCAPTCHA solved by checkbox");
        process.exit(0);
    } catch {
        // A challenge is required. Wait for its iframe before invoking the solver.
    }

    await page.waitForSelector(challengeFrameSelector, {
        state: "visible",
        timeout: 15000,
    });
    const solved = await solve(solverCompatiblePage(page));
    console.log(`reCAPTCHA solved: ${solved}`);
    process.exit(0);
} catch (error) {
    console.error(error instanceof Error ? error.stack : String(error));
    process.exit(1);
}
