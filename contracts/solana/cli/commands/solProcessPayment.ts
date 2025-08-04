import { BN, Program } from "@coral-xyz/anchor";
import {
  Connection,
  Keypair,
  PublicKey,
  sendAndConfirmTransaction,
  SystemProgram,
  Transaction,
} from "@solana/web3.js";
import { program } from "..";
import idl from "../../target/idl/libertai_payment_processor.json";
import { LibertaiPaymentProcessor } from "../../target/types/libertai_payment_processor";
import { getKeypair } from "../utils";

const solProcessPayment = async (
  payer: Keypair,
  amount: BN,
  program: Program
) => {
  const userWallet = payer.publicKey;

  const [programState] = PublicKey.findProgramAddressSync(
    [Buffer.from("program_state")],
    program.programId
  );

  const ix = await program.methods
    .processPaymentSol(amount)
    .accounts({
      user: userWallet,
      programState: programState,
      systemProgram: SystemProgram.programId,
    })
    .instruction();

  const tx = new Transaction().add(ix);
  const sig = await sendAndConfirmTransaction(program.provider.connection, tx, [payer]);
  console.log(`✅ SOL payment processed. Tx Signature: ${sig}`);

  return sig;
}

export const SolProcessPaymentCommand = async () => {
  const opts = program.opts();

  const payer = getKeypair({
    filepath: opts.payerKeyFilepath,
    key: opts.payerPrivateKey,
  });

  const connection = new Connection(opts.jsonRpcEndpoint, "confirmed");
  const anchorProgram = new Program(idl as LibertaiPaymentProcessor, {
    connection,
  });

  const humanAmount = parseFloat(opts.amount);
  const lamports = new BN(humanAmount * 1e9); // Convert SOL to lamports

  console.log(`Processing SOL payment of ${humanAmount} SOL (${lamports.toString()} lamports)...`);

  try {
    const signature = await solProcessPayment(payer, lamports, anchorProgram);
    console.log(`✅ SOL payment completed successfully!`);
  } catch (error) {
    console.error("❌ Failed to process SOL payment:", error);
    process.exit(1);
  }
};
