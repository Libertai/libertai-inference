import { Connection, PublicKey } from "@solana/web3.js";
import { program } from "..";
import { getKeypair } from "../utils";
import { Program } from "@coral-xyz/anchor";
import { LibertAiPaymentProcessor } from "../../target/types/libert_ai_payment_processor";
import idl from "../../target/idl/libert_ai_payment_processor.json";

const getAdmins = async (
  anchorProgram: Program<LibertAiPaymentProcessor>
) => {
  const [programStatePDA] = PublicKey.findProgramAddressSync(
    [Buffer.from("program_state")],
    anchorProgram.programId
  );

  const programState = await anchorProgram.account.programState.fetch(programStatePDA);
  
  console.log(programState.admins.length > 0 ? "Admins:" : "There are no admins except the owner");
  programState.admins.forEach((admin, index) => {
    console.log(`${index + 1}: ${admin.toString()}`);
  });
};

export const GetAdminsCommand = async () => {
  const opts = program.opts();

  const connection = new Connection(opts.jsonRpcEndpoint, "confirmed");
  const anchorProgram = new Program(idl as LibertAiPaymentProcessor, {
    connection,
  }) as Program<LibertAiPaymentProcessor>;

  await getAdmins(anchorProgram);
};
