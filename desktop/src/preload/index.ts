import "./avatar";
import "./chat";
import "./panel";
import { readWindowBootstrapArgument } from "../shared/windowBootstrap";

const hasSupportedWindowBootstrap =
  readWindowBootstrapArgument(process.argv, "avatar") !== null ||
  readWindowBootstrapArgument(process.argv, "panel") !== null ||
  readWindowBootstrapArgument(process.argv, "chat") !== null;

if (!hasSupportedWindowBootstrap) {
  throw new Error("window bootstrap argument is missing or invalid");
}
