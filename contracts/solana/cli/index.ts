import { Command } from "commander";
import { InitializeCommand } from "./commands/initialize";
import { ProcessPaymentCommand } from "./commands/processPayment";
import { CreateProgramTokenAccountCommand } from "./commands/createProgramTokenAccount";

export const program = new Command();

program
  .name("libert")
  .description("Libert AI Payment Processor CLI")
  .option("--payer-key-filepath <path>", "Path to payer private key JSON file")
  .option("--payer-private-key <key>", "Payer private key as JSON string")
  .option("--json-rpc-endpoint <url>", "Solana RPC endpoint", "https://api.testnet.solana.com")
  .option("--amount <amount>", "Amount of tokens to transfer (in smallest unit, e.g. lamports or decimals)")
  .option("--token-mint <address>", "Token mint address", "HrGxyLboQpUAxQTDm5AKQ2vfEASo1FNnRFY3AqEi3iDk")


program
  .command("initialize")
  .description("Initialize the program")
  .action(InitializeCommand);

program
  .command("process_payment")
  .description("Process a payment and emit an event")
  .action(ProcessPaymentCommand);

program
  .command("create_program_token_account")
  .description("Creates program token account")
  .action(CreateProgramTokenAccountCommand)

program.parseAsync(process.argv);
